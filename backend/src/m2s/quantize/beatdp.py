"""Tempo-consistent beat tracking over beat_this activations (stage S3).

beat_this's peak-picked beats switch metrical level inside a piece (e.g. 65 → 130 →
30 bpm in a rubato ballad), which wrecks quantization. Here we:

1. pick one global tempo *level* (estimate_level): candidate tempi come from the
   tracker's own local tempi and the activation autocorrelation, each at ×½/×1/×2;
   every candidate is tracked with the DP below and scored by how well its beats
   explain the activation peaks (F-measure of precision/recall against the peaks),
   a log-normal tempo prior and the plausibility of note-onset events per beat;
2. track beats at that level with an Ellis-style dynamic programme over the frame
   activations (dp_track). Each beat interval is penalised for deviating from the
   previous interval (continuity: drift/rubato allowed, octave jumps are not) and,
   weakly, from the target period. Intervals are hard-limited to a band around the
   target so the level cannot switch;
3. choose the bar grid (beats per bar + phase) from the downbeat activation sampled
   at the new beats, so downbeats are consistent with the new beat level.

A user bpm prior replaces step 1 (so "tempo ×2 / ÷2" in the editor just reruns with
priors.bpm doubled/halved).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# ---- DP parameters --------------------------------------------------------------
BAND = (0.6, 1.67)  # allowed beat interval as a multiple of the target period
CONTINUITY = 4.0  # weight on log(ibi / previous ibi)^2
TARGET_PULL = 1.5  # weight on log(ibi / target)^2
DOWNBEAT_W = 0.35  # downbeat activation also rewards a beat (phase at slow levels)
STEP_MAX = np.log(1.4)  # hard limit on the interval change from one beat to the next
REWARD_BIAS = 0.3  # subtracted from the activation: a beat on nothing costs this much
# ---- level selection ------------------------------------------------------------
BPM_RANGE = (28.0, 260.0)
# Log-normal priors measured on the 356 ASAP performances' annotated (notated) beats:
# tempo median 105 bpm, sd 0.77 octave; rhythmic events (onsets ≥ 50 ms apart) per
# beat median 3.56, sd ≈ 0.6 octave (long upper tail). The density prior is the sharper
# of the two: the notated beat is the one that makes most notes eighths/sixteenths.
PRIOR_CENTER_BPM, PRIOR_SIGMA_OCT = 105.0, 0.77
EVENTS_CENTER, EVENTS_SIGMA_OCT = 3.56, 0.6
EVENTS_SIGMA_HI_OCT = 1.0
# weight of log F-measure between level beats and activation peaks, from FIT_W_LO when
# the best level's fit is ≤ TRUST_FIT[0] to FIT_W_HI when it is ≥ TRUST_FIT[1]
FIT_W_LO, FIT_W_HI, TRUST_FIT = 2.0, 8.0, (0.8, 0.9)
MIX_W = 4.0  # penalty per share of beats at a different local level (see _score_level)
LEVEL_TOL = 0.4  # octaves
DOWNBEAT_FIT_W = 3.0  # weight of log share of downbeat peaks that land on level beats
CLUSTER_S = 0.05  # onsets closer than this are one rhythmic event
PEAK_TOL_S = 0.07
# ---- meter ----------------------------------------------------------------------
FOUR_VS_TWO = 0.73  # see choose_bar_grid
# switching the bar phase costs this much downbeat salience (≈ 4–5 bars of clear
# evidence); None disables the repair. See repair_bar_phase and ADR 0006.
PHASE_REPAIR: float | None = 2.0
MIN_CONTRAST = 0.03


@dataclass
class Activations:
    beat: np.ndarray  # beat probability per frame
    downbeat: np.ndarray  # downbeat probability per frame
    fps: float


@dataclass
class LevelCandidate:
    bpm: float  # DP target
    score: float
    fit: float
    downbeat_recall: float
    mixed: float
    prior: float
    density: float
    events_per_beat: float
    tracked_bpm: float


@dataclass
class TrackResult:
    beats: np.ndarray  # seconds
    downbeats: list[float]  # seconds, a subset of beats
    bpm: float  # median tracked tempo (beats per minute at the tracked level)
    target_bpm: float
    confidence: float  # 0..1: how clearly the chosen level beat its octave alternatives
    from_prior: bool
    beats_per_bar: int
    candidates: list[LevelCandidate] = field(default_factory=list)


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -50, 50)))


def load_activations(path: str | Path | None) -> Activations | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    with np.load(p) as z:
        return Activations(
            beat=_sigmoid(z["beat"].astype(float)),
            downbeat=_sigmoid(z["downbeat"].astype(float)),
            fps=float(z["fps"]) if "fps" in z else 50.0,
        )


# ---------------------------------------------------------------------------------
# DP
# ---------------------------------------------------------------------------------


def dp_track(reward: np.ndarray, period: float) -> np.ndarray:
    """Best beat sequence (frame indices) through `reward` at a target `period` (frames).

    score[t] = reward[t] + max_p (score[p] − C·log²(g/ibi[p]) − T·log²(g/period)), g = t − p,
    where ibi[p] is the interval that led to p (second-order continuity, carried along
    the best path), g is limited to BAND·period and |log(g/ibi[p])| to STEP_MAX.
    Predecessors are ≥ lo frames back, so blocks of lo frames are solved at once
    (vectorised: ~20 ms per candidate for a 6-minute piece).
    """
    n = len(reward)
    lo = max(2, int(np.floor(BAND[0] * period)))
    hi = max(lo + 1, int(np.ceil(BAND[1] * period)))
    if n <= hi:
        return np.array([int(np.argmax(reward))]) if n else np.array([], dtype=int)
    score = np.empty(n)
    back = np.full(n, -1)
    ibi = np.full(n, float(period))
    gaps = np.arange(lo, hi + 1)
    target_pen = TARGET_PULL * np.log(gaps / period) ** 2
    score[:lo] = reward[:lo]
    for start in range(lo, n, lo):
        ts = np.arange(start, min(start + lo, n))
        rows = np.arange(len(ts))
        ps = ts[:, None] - gaps[None, :]
        valid = ps >= 0
        psc = np.where(valid, ps, 0)
        step = np.log(gaps[None, :] / ibi[psc])
        cand = score[psc] - CONTINUITY * step**2 - target_pen[None, :]
        cand = np.where(valid & (np.abs(step) <= STEP_MAX), cand, -np.inf)
        j = np.argmax(cand, axis=1)
        best = cand[rows, j]
        # a chain may (re)start within the first `hi` frames
        use_prev = np.isfinite(best) & ~((ts < hi) & (best < 0))
        score[ts] = reward[ts] + np.where(use_prev, best, 0.0)
        back[ts] = np.where(use_prev, psc[rows, j], -1)
        ibi[ts] = np.where(use_prev, gaps[j], period)
    end = n - hi + int(np.argmax(score[n - hi :]))
    out = [end]
    while back[out[-1]] >= 0:
        out.append(int(back[out[-1]]))
    return np.array(out[::-1])


def beat_tempo(beats: np.ndarray) -> float | None:
    """Typical tempo (bpm) of a beat track: the median of 8-beat average intervals. Beats sit
    on 20-ms frames, so a plain median interval is coarse at fast tempi (180 bpm read as
    176.5); averaging over 8 intervals removes that rounding."""
    ibi = np.diff(np.asarray(beats, dtype=float))
    if len(ibi) < 2:
        return None
    w = min(8, len(ibi))
    return float(60.0 / np.median(np.convolve(ibi, np.ones(w) / w, "valid")))


def _local_max(x: np.ndarray, frames: np.ndarray, w: int = 2) -> np.ndarray:
    lo = np.clip(frames - w, 0, len(x))
    hi = np.clip(frames + w + 1, 0, len(x))
    return np.array([x[a:b].max() if b > a else 0.0 for a, b in zip(lo, hi)])


# ---------------------------------------------------------------------------------
# tempo level
# ---------------------------------------------------------------------------------


def _peaks(x: np.ndarray, f0: int, f1: int) -> np.ndarray:
    """Frames of activation peaks, as beat_this's own 'minimal' post-processing picks them
    (local max within ±70 ms, probability > 0.5)."""
    seg = x[f0:f1]
    if len(seg) < 3:
        return np.array([], dtype=int)
    pad = np.pad(seg, 3, constant_values=0.0)
    win = np.lib.stride_tricks.sliding_window_view(pad, 7).max(axis=1)
    pk = np.flatnonzero((seg >= win) & (seg > 0.5))
    if len(pk):
        pk = pk[np.concatenate([[True], np.diff(pk) > 1])]
    return pk + f0


def _candidate_bpms(act: Activations, f0: int, f1: int) -> list[float]:
    """Tempo hypotheses: the tracker's dominant local tempi + activation periodicities, ×½/×1/×2."""
    seeds: list[tuple[float, float]] = []  # (bpm, weight)
    rb = _peaks(act.beat, f0, f1) / act.fps
    if len(rb) >= 4:
        ibi = np.diff(rb)
        ok = (ibi > 60 / BPM_RANGE[1] / 2) & (ibi < 60 / BPM_RANGE[0] * 2)
        if ok.any():
            lb = np.log2(60 / ibi[ok])
            bins = np.arange(3.5, 9.5, 1 / 24)
            h, e = np.histogram(lb, bins=bins, weights=ibi[ok])
            h = np.convolve(h, np.hanning(7), "same")
            peaks = [i for i in range(1, len(h) - 1) if h[i] >= h[i - 1] and h[i] > h[i + 1]]
            for i in sorted(peaks, key=lambda i: -h[i])[:3]:
                seeds.append((2 ** ((e[i] + e[i + 1]) / 2), float(h[i])))
    x = act.beat[f0:f1] - act.beat[f0:f1].mean()
    if len(x) > 4 * act.fps:
        spec = np.fft.rfft(x, 2 * len(x))
        ac = np.fft.irfft(spec * np.conj(spec))[: len(x)]
        lags = np.arange(int(act.fps * 60 / BPM_RANGE[1]), min(len(ac) - 1, int(act.fps * 60 / BPM_RANGE[0])))
        pk = [k for k in lags[1:] if ac[k] > ac[k - 1] and ac[k] >= ac[k + 1] and ac[k] > 0]
        for k in sorted(pk, key=lambda k: -ac[k])[:2]:
            seeds.append((60 * act.fps / k, float(ac[k])))
    cands: list[float] = []
    for bpm, _ in seeds:
        for m in (0.5, 1.0, 2.0):
            c = bpm * m
            if BPM_RANGE[0] <= c <= BPM_RANGE[1] and all(abs(np.log(c / o)) > 0.08 for o in cands):
                cands.append(c)
    return cands or [100.0]


def _events(onsets: np.ndarray) -> np.ndarray:
    """Collapse near-simultaneous onsets (chords, rolled chords) into rhythmic events."""
    on = np.sort(onsets)
    if len(on) == 0:
        return on
    keep = np.concatenate([[True], np.diff(on) > CLUSTER_S])
    return on[keep]


def _reward(act: Activations, f0: int, f1: int) -> np.ndarray:
    return act.beat[f0:f1] + DOWNBEAT_W * act.downbeat[f0:f1] - REWARD_BIAS


def _recall(peaks: np.ndarray, x: np.ndarray, frames: np.ndarray, tol: int) -> float:
    """Activation-weighted share of `peaks` that have a beat within `tol` frames."""
    if not len(peaks) or not len(frames):
        return 0.0
    j = (
        np.clip(np.searchsorted(frames, peaks), 1, len(frames) - 1)
        if len(frames) > 1
        else np.zeros(len(peaks), int)
    )
    d = np.minimum(np.abs(frames[j] - peaks), np.abs(frames[np.maximum(j - 1, 0)] - peaks))
    h = x[peaks]
    return float(np.sum(h * (d <= tol)) / max(np.sum(h), 1e-9))


def _density_logprior(epb: float) -> float:
    """Log-normal prior on rhythmic events per beat; the upper side is wider because
    virtuoso textures (Liszt, Ravel) legitimately reach 7–11 events per notated beat."""
    d = np.log2(max(epb, 0.05) / EVENTS_CENTER)
    return float(-0.5 * (d / (EVENTS_SIGMA_OCT if d < 0 else EVENTS_SIGMA_HI_OCT)) ** 2)


def _score_level(
    act: Activations, f0: int, f1: int, bpm: float, events: np.ndarray
) -> tuple[LevelCandidate, np.ndarray]:
    """Track at target `bpm` and measure the evidence for that level (scored by _rescore)."""
    period = act.fps * 60.0 / bpm
    frames = dp_track(_reward(act, f0, f1), period) + f0
    beats_s = frames / act.fps
    tol = int(round(PEAK_TOL_S * act.fps))
    prec = float(np.mean(_local_max(act.beat, frames, tol))) if len(frames) else 0.0
    rec = _recall(_peaks(act.beat, f0, f1), act.beat, frames, tol)
    fit = 2 * prec * rec / max(prec + rec, 1e-9)
    # bars must be whole numbers of beats: a level that splits the tracker's bars (e.g.
    # half tempo in 3/4) leaves downbeat peaks between beats
    db_peaks = _peaks(act.downbeat, f0, f1)
    db_rec = _recall(db_peaks, act.downbeat, frames, tol) if len(db_peaks) >= 4 else 1.0
    tracked = 60.0 / float(np.median(np.diff(beats_s))) if len(beats_s) > 2 else bpm
    # level consistency: share of beats whose local tempo (median of 8 intervals) is off
    # the track's median by more than LEVEL_TOL octaves — i.e. the track mixes two levels
    if len(beats_s) > 10:
        lt = np.log2(np.diff(beats_s))
        k = 8
        loc = np.array([np.median(lt[max(0, i - k // 2) : i + k // 2 + 1]) for i in range(len(lt))])
        mixed = float(np.mean(np.abs(loc - np.median(lt)) > LEVEL_TOL))
    else:
        mixed = 0.0
    prior = -0.5 * (np.log2(tracked / PRIOR_CENTER_BPM) / PRIOR_SIGMA_OCT) ** 2
    epb = len(events) / max(len(beats_s), 1)
    dens = _density_logprior(epb)
    cand = LevelCandidate(
        bpm=round(bpm, 2),
        score=0.0,
        fit=round(fit, 4),
        downbeat_recall=round(db_rec, 4),
        mixed=round(mixed, 4),
        prior=round(float(prior), 4),
        density=round(float(dens), 4),
        events_per_beat=round(epb, 3),
        tracked_bpm=round(tracked, 2),
    )
    return cand, beats_s


def _rescore(c: LevelCandidate, fit_w: float) -> LevelCandidate:
    c.score = round(
        float(
            -MIX_W * c.mixed
            + fit_w * np.log(max(c.fit, 1e-3))
            + DOWNBEAT_FIT_W * np.log(max(c.downbeat_recall, 0.05))
            + c.prior
            + c.density
        ),
        4,
    )
    return c


def _confidence(best: LevelCandidate, cands: list[LevelCandidate]) -> float:
    """Margin over the best candidate at a different metrical level (ratio ≳ 1.3)."""
    alts = [c.score for c in cands if abs(np.log2(c.tracked_bpm / best.tracked_bpm)) > 0.4]
    if not alts:
        return 1.0
    return float(np.round(1.0 - np.exp(-max(best.score - max(alts), 0.0)), 3))


# ---------------------------------------------------------------------------------
# meter
# ---------------------------------------------------------------------------------


def choose_bar_grid(
    salience: np.ndarray, beats_per_bar: int | None = None
) -> tuple[int, int, dict[int, float]]:
    """(beats_per_bar, phase, best contrast per bpb) from downbeat salience at each beat.

    Contrast = mean salience on the bar positions − mean elsewhere. When downbeat peaks
    come every 2 beats and are equally strong, the 4-beat grid's contrast is exactly 2/3
    of the 2-beat one (4 = 2 + 2). beat_this often marks half-bars of a slow 4/4 as
    downbeats too, but more weakly; that alternation lifts the ratio above 2/3, so 4
    wins once its contrast reaches FOUR_VS_TWO × the 2-beat contrast. Measured on 55
    ASAP/MAESTRO performances + the user's two ballads (ADR 0006), the ratios of 2- and
    4-beat bars overlap between 0.70 and 0.76; 0.73 keeps the most margin to both sides
    of the pieces it gets right: 2-beat bars at 0.708 (Berceuse 6/8), 0.713 (Schubert
    D780/3), 0.717 (D935/3) and the user's 4/4 ballads at 0.751 and 0.775. Still wrong
    either way: Appassionata 12/8 (4 beats, 0.718) and Bach BWV 893 (2/2, 0.755).
    0.70 put the Berceuse in 12/8 with half its bar lines mid-bar."""
    n = len(salience)
    best: dict[int, tuple[float, int]] = {}
    for bpb in (beats_per_bar,) if beats_per_bar else (2, 3, 4):
        for ph in range(bpb):
            mask = np.zeros(n, bool)
            mask[ph::bpb] = True
            if not mask.any() or mask.all():
                continue
            c = float(salience[mask].mean() - salience[~mask].mean())
            if bpb not in best or c > best[bpb][0]:
                best[bpb] = (c, ph)
    contrast = {k: round(v[0], 4) for k, v in best.items()}
    if not best:
        return beats_per_bar or 4, 0, contrast
    if beats_per_bar:
        return beats_per_bar, best[beats_per_bar][1], contrast
    score = {k: c / FOUR_VS_TWO if k == 4 else c for k, (c, _) in best.items()}
    bpb = max(score, key=lambda k: score[k])
    if best[bpb][0] < MIN_CONTRAST:  # no usable downbeat evidence
        bpb = 4 if 4 in best else bpb
    return bpb, best[bpb][1], contrast


def repair_bar_phase(
    beats: np.ndarray, sal: np.ndarray, beat_act: np.ndarray, bpb: int, phase: int, penalty: float
) -> tuple[np.ndarray, int]:
    """Re-synchronise the bar grid where the beat track gained or lost one beat.

    The DP can put an extra beat into a fermata or ritardando (or drop one in a
    stretto); every bar line after it then sits one beat off (Schubert D899/4: bars
    right for 40 s, then one beat late). A Viterbi over each beat's position in the bar
    may, at a cost of `penalty` (in downbeat-salience units, i.e. several bars of clear
    evidence), stay on the same position (an extra beat → the weaker of the two is
    removed) or skip one (a missing beat → one is inserted halfway). Only for 3 and 4
    beats per bar: with 2, "extra" and "missing" are the same move and a half-bar shift
    is as likely. Returns the repaired beats and the phase (index of a downbeat)."""
    n = len(beats)
    if bpb < 3 or n < 4 * bpb or penalty is None:
        return beats, phase
    on = np.zeros(n, bool)
    on[phase::bpb] = True
    thr = 0.5 * (sal[on].mean() + sal[~on].mean())
    emit = np.zeros((n, bpb))
    emit[:, 0] = sal - thr
    score = emit[0].copy()
    back = np.zeros((n, bpb), dtype=np.int8)  # 0 normal, 1 extra beat, 2 missing beat
    pos = np.arange(bpb)
    for k in range(1, n):
        cand = np.stack([score[(pos - 1) % bpb], score - penalty, score[(pos - 2) % bpb] - penalty])
        j = np.argmax(cand, axis=0)
        back[k] = j
        score = cand[j, pos] + emit[k]
    p = int(np.argmax(score))
    path = np.empty(n, int)
    moves = np.zeros(n, np.int8)
    for k in range(n - 1, -1, -1):
        path[k] = p
        moves[k] = back[k, p] if k else 0
        p = (p - 1) % bpb if moves[k] == 0 else p if moves[k] == 1 else (p - 2) % bpb
    if not moves.any():
        return beats, phase
    out: list[float] = []
    for k in range(n):
        if moves[k] == 1:  # beats k-1 and k share a bar position: keep the stronger one
            if beat_act[k] > beat_act[k - 1]:
                out[-1] = float(beats[k])
            continue
        if moves[k] == 2:
            out.append(float(beats[k - 1] + beats[k]) / 2)
        out.append(float(beats[k]))
    fixed = np.array(out)
    first = 0 if path[0] == 0 else bpb - path[0]
    return fixed, first % bpb


# ---------------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------------


def track(
    act: Activations,
    onsets: np.ndarray,
    t_first: float,
    t_last: float,
    bpm_prior: float | None = None,
    beats_per_bar: int | None = None,
) -> TrackResult:
    """Tempo-consistent beats (+ downbeats) over [t_first, t_last] (± one beat margin)."""
    f0 = max(0, int(np.floor((t_first - 1.0) * act.fps)))
    f1 = min(len(act.beat), int(np.ceil((t_last + 1.0) * act.fps)))
    events = _events(np.asarray(onsets, dtype=float))
    if bpm_prior:
        cand, beats = _score_level(act, f0, f1, float(bpm_prior), events)
        cands, conf, from_prior = [cand], 1.0, True
    else:
        bpms = _candidate_bpms(act, f0, f1)
        scored = [_score_level(act, f0, f1, c, events) for c in bpms]
        # how much to trust the tracker's own level: a tracker that follows one level
        # consistently (best fit ≥ ~0.9, typical for steady classical pieces) outweighs
        # the musical priors; one that wanders between levels (fit ≤ ~0.8) does not
        best_fit = max(c.fit for c, _ in scored)
        w = FIT_W_LO + (FIT_W_HI - FIT_W_LO) * float(
            np.clip((best_fit - TRUST_FIT[0]) / (TRUST_FIT[1] - TRUST_FIT[0]), 0, 1)
        )
        scored = [(_rescore(c, w), b) for c, b in scored]
        scored.sort(key=lambda x: -x[0].score)
        cand, beats = scored[0]
        cands = [c for c, _ in scored]
        conf, from_prior = _confidence(cand, cands), False
    frames = np.round(beats * act.fps).astype(int)
    sal = _local_max(act.downbeat, frames, 2)
    bpb, phase, _ = choose_bar_grid(sal, beats_per_bar)
    if PHASE_REPAIR is not None:
        beats, phase = repair_bar_phase(beats, sal, _local_max(act.beat, frames, 2), bpb, phase, PHASE_REPAIR)
    downbeats = [float(b) for b in beats[phase::bpb]]
    return TrackResult(
        beats=beats,
        downbeats=downbeats,
        bpm=round(beat_tempo(beats) or cand.tracked_bpm, 2),
        target_bpm=cand.bpm,
        confidence=conf,
        from_prior=from_prior,
        beats_per_bar=bpb,
        candidates=cands,
    )
