"""Stage S4: performance (seconds) + beat grid → quantized ScoreDoc (staffs unassigned).

Steps: cluster near-simultaneous onsets → beat positions → Viterbi subdivision per
beat → meter/phase → pickup → ticks. Offsets snap to their beat's grid; notes are
never lengthened by the sustain pedal (pedals become separate ScorePedal marks).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

import numpy as np

from m2s.models.artifacts import TempoMap, Transcription
from m2s.models.priors import Priors
from m2s.models.scoredoc import PPQ, KeyChange, MeterChange, ScoreDoc, ScoreNote, ScorePedal, TempoMark
from m2s.quantize.beatdp import beat_tempo
from m2s.quantize.beatmap import BeatMap
from m2s.quantize.meter import Meter, from_time_sig, infer_meter
from m2s.quantize.subdiv_dp import COMPOUND, SIMPLE, choose_subdivisions, snap_to_beat_grid

# chord-onset clustering window: a fraction of the finest grid unit, clamped (seconds)
CLUSTER_FRAC, CLUSTER_MIN_S, CLUSTER_MAX_S = 0.35, 0.025, 0.06
# how many native subdivision levels the minimum grid allows (1/16 → quarter÷4 or dotted-quarter÷6)
GRID_LEVEL = {"1/8": 1, "1/16": 2, "1/32": 3}
# simple meters are ~4.5× more common than compound ones (ASAP): compound must win the
# rhythm fit by this much per beat that has onsets. Tuned with
# scripts/sweep_compound_bias.py on 228 ASAP scores: 0.8 → meter 77.6%, all 37 compound
# scores right, 19/191 simple scores read as compound (mostly triplet-heavy pieces).
COMPOUND_BIAS = 0.8
# Beats slower than this (< 48 bpm) are too slow for the ÷8 vs ÷12 comparison: after
# "tempo ÷2" a ballad's sixteenths are 64ths of the new beat, which the compound ÷12 grid
# happens to fit better than the simple ÷8 one (Call of Silence ÷2 came out in 12/8).
# Such beats are also judged on the half-beat grid; two simple half-beats make a simple
# beat, so a simple reading there wins. Compound music (Berceuse at ♩.≈35) stays compound
# there because its eighths fall on thirds of the half-beats.
SLOW_BEAT_S = 1.25


@dataclass
class QuantStats:
    n_beats: int
    meter: str
    meter_score: float
    model: str  # "simple" or "compound" beat
    cost_simple: float | None
    cost_compound: float | None
    pickup_beats: int
    subdiv_hist: dict[int, int] = field(default_factory=dict)


def cluster_onsets(onsets: np.ndarray, tol: float) -> tuple[np.ndarray, np.ndarray]:
    """Group sorted onsets whose distance to the cluster's first onset is ≤ tol.

    Returns (cluster mean times, cluster index per onset)."""
    order = np.argsort(onsets)
    labels = np.empty(len(onsets), dtype=int)
    means: list[list[float]] = []
    start = None
    for i in order:
        t = onsets[i]
        if start is None or t - start > tol:
            means.append([])
            start = t
        means[-1].append(t)
        labels[i] = len(means) - 1
    return np.array([np.mean(m) for m in means]), labels


def quantize(
    tr: Transcription, beats: np.ndarray, downbeats_s: list[float], priors: Priors
) -> tuple[ScoreDoc, QuantStats]:
    notes = sorted(tr.notes, key=lambda n: (n.onset_s, n.pitch))
    if not notes:
        raise ValueError("no notes transcribed")
    onsets = np.array([n.onset_s for n in notes])
    offsets = np.array([n.offset_s for n in notes])
    bm = BeatMap(beats, float(onsets.min()), float(offsets.max()))
    n_beats = len(bm.beats) - 1

    level = GRID_LEVEL[priors.min_grid]
    med_ibi = float(np.median(np.diff(bm.beats)))
    tol = float(np.clip(CLUSTER_FRAC * med_ibi / 2**level, CLUSTER_MIN_S, CLUSTER_MAX_S))
    centers, labels = cluster_onsets(onsets, tol)
    cpos = bm.pos(centers)

    def fit(m, lvl: int, grid: BeatMap = bm):
        pos = cpos if grid is bm else grid.pos(centers)
        return choose_subdivisions(
            pos,
            grid.ibi,
            len(grid.beats) - 1,
            m.candidates(lvl, priors.triplets),
            m,
            tuplet_scale=0.7 if priors.style == "classical" else 1.0,
            cluster_tol_s=tol,
        )

    def compound_wins(grid: BeatMap) -> tuple[bool, dict[str, float]]:
        c = {m.name: fit(m, 3, grid).cost for m in (SIMPLE, COMPOUND)}
        busy = len({int(np.floor(p)) for p in (cpos if grid is bm else grid.pos(centers))})
        return c["compound"] + COMPOUND_BIAS * busy < c["simple"], c

    # Simple vs compound beat: forced by a time-signature prior, else the cheaper rhythm
    # fit. The comparison runs at the finest level (÷8 vs ÷12) so that music finer than
    # the user's grid doesn't favour whichever model happens to have the finer grid.
    costs: dict[str, float] = {}
    if priors.time_sig:
        model = COMPOUND if from_time_sig(priors.time_sig)[3] == PPQ * 3 // 2 else SIMPLE
    else:
        compound, costs = compound_wins(bm)
        if compound and med_ibi > SLOW_BEAT_S:
            b = bm.beats
            halves = BeatMap(np.sort(np.concatenate([b, (b[:-1] + b[1:]) / 2])), b[0], b[-1])
            compound = compound_wins(halves)[0]
        model = COMPOUND if compound else SIMPLE
    res = fit(model, level)
    model_name = model.name
    meter: Meter = infer_meter(
        bm.downbeat_indices(downbeats_s), n_beats, priors.time_sig, compound=model_name == "compound"
    )
    bt = meter.beat_ticks

    start_beat = int(np.floor(min(res.snapped)))
    pickup_beats = (meter.phase - start_beat) % meter.beats_per_bar

    def to_tick(pos: Fraction) -> int:
        t = (pos - start_beat) * bt
        return int(round(t))

    score_notes: list[ScoreNote] = []
    for i, n in enumerate(notes):
        c = labels[i]
        on_pos = res.snapped[c]
        k_on = min(int(np.floor(on_pos)), n_beats - 1)
        s_on = res.subdiv[k_on]
        min_dur = bt // (s_on if s_on > 1 else 2)
        off_pos = snap_to_beat_grid(float(bm.pos(n.offset_s)), res.subdiv, floor_s=2)
        start = to_tick(on_pos)
        dur = max(to_tick(off_pos) - start, min_dur)
        err_s = abs(float(bm.time(float(on_pos))) - n.onset_s)
        conf = 1.0 - 0.5 * min(err_s / 0.12, 1.0) - (0.3 if n.velocity < 20 else 0.0)
        score_notes.append(
            ScoreNote(
                id=f"n{i:05d}",
                pitch=n.pitch,
                start=start,
                dur=dur,
                velocity=n.velocity,
                confidence=round(conf, 3),
                src_ids=[n.id],
            )
        )
    score_notes = _resolve_same_pitch_overlaps(score_notes)

    pedals = _quantize_pedals(tr, bm, start_beat, bt)
    anchors = [((k - start_beat) * bt, float(bm.beats[k])) for k in range(len(bm.beats))]
    beat_bpm = beat_tempo(bm.beats[max(start_beat, 0) :]) or 100.0
    # TempoMark.bpm counts quarter notes; a beat of bt ticks is bt/PPQ quarters
    # (dotted quarter: ×1.5, half: ×2)
    quarter_bpm = beat_bpm * bt / PPQ
    doc = ScoreDoc(
        pickup_ticks=pickup_beats * bt,
        meters=[MeterChange(tick=0, num=meter.num, den=meter.den)],
        keys=[KeyChange(tick=0, fifths=0)],
        tempos=[TempoMark(tick=0, bpm=round(quarter_bpm, 1))],
        notes=score_notes,
        pedals=pedals,
        tempo_map=TempoMap(anchors=anchors),
    )
    hist: dict[int, int] = {}
    for s in res.subdiv:
        hist[s] = hist.get(s, 0) + 1
    stats = QuantStats(
        n_beats=n_beats,
        meter=f"{meter.num}/{meter.den}",
        meter_score=round(meter.score, 3),
        model=model_name,
        cost_simple=round(costs["simple"], 1) if costs else None,
        cost_compound=round(costs["compound"], 1) if costs else None,
        pickup_beats=pickup_beats,
        subdiv_hist=hist,
    )
    return doc, stats


def requantize(
    notes: list[ScoreNote],
    onset_s: dict[str, float],
    tempo_map: TempoMap,
    beat_ticks: int,
    cands: list[int],
    model,
    cluster_tol_s: float,
    tuplet_scale: float = 1.0,
) -> None:
    """Re-snap the onsets of `notes` (in place) onto a new subdivision grid, keeping the
    doc's beats, meter and tick scale. Used by notation/simplify.py on the notes it keeps:
    `onset_s` holds their performed onsets (note id → seconds); a note without one uses
    its current tick. Ends stay where they were (callers re-derive durations)."""
    if not notes:
        return
    anchors = sorted(tempo_map.anchors)
    secs = np.array([a[1] for a in anchors], dtype=float)
    tick0 = anchors[0][0]  # anchor i sits at tick tick0 + i * beat_ticks
    t = np.array([onset_s.get(n.id, tempo_map.tick_to_sec(n.start)) for n in notes])
    centers, labels = cluster_onsets(t, cluster_tol_s)
    cpos = np.interp(centers, secs, np.arange(len(secs), dtype=float))
    n_beats = len(secs) - 1

    def ibi(k: int) -> float:
        k = int(np.clip(k, 0, n_beats - 1))
        return float(secs[k + 1] - secs[k])

    res = choose_subdivisions(cpos, ibi, n_beats, cands, model, tuplet_scale, cluster_tol_s)
    for n, c in zip(notes, labels):
        end = n.start + n.dur
        n.start = max(0, int(round(tick0 + res.snapped[c] * beat_ticks)))
        n.dur = max(end - n.start, 1)


def _resolve_same_pitch_overlaps(notes: list[ScoreNote]) -> list[ScoreNote]:
    """Same pitch can't sound twice: truncate the earlier note; drop exact duplicates."""
    out: list[ScoreNote] = []
    by_pitch: dict[int, ScoreNote] = {}
    for n in sorted(notes, key=lambda n: (n.start, -n.velocity)):
        prev = by_pitch.get(n.pitch)
        if prev is not None and prev.start + prev.dur > n.start:
            if prev.start == n.start:
                continue  # duplicate onset after quantization: keep the louder (first) one
            prev.dur = n.start - prev.start
        by_pitch[n.pitch] = n
        out.append(n)
    return out


def _quantize_pedals(tr: Transcription, bm: BeatMap, start_beat: int, bt: int) -> list[ScorePedal]:
    unit = bt // 4
    out: list[ScorePedal] = []
    for p in sorted(tr.pedals, key=lambda p: (p.kind, p.start_s)):
        s = int(round((float(bm.pos(p.start_s)) - start_beat) * 4)) * unit
        e = int(round((float(bm.pos(p.end_s)) - start_beat) * 4)) * unit
        s = max(s, 0)
        prev = out[-1] if out and out[-1].kind == p.kind else None
        if prev is not None and s - prev.end < unit:
            # re-pedalling: keep both, abut them (rendered as a pedal "change")
            s = max(s, prev.end)
            prev.end = s
        if e - s < bt // 2:
            continue
        out.append(ScorePedal(id=f"p{len(out):04d}", kind=p.kind, start=s, end=e))
    return out
