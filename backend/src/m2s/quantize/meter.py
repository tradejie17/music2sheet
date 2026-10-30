"""Meter inference: beats-per-bar + phase from (unreliable) tracker downbeats.

The M0 spike showed beat_this downbeats on rubato classical piano jump between 1–4
beats per bar, so we never trust individual downbeats. Instead we pick the regular
(beats_per_bar, phase) grid that best agrees with them (F-measure), honouring the
user's time-signature prior when given.
"""

from __future__ import annotations

from dataclasses import dataclass

from m2s.models.scoredoc import PPQ


@dataclass(frozen=True)
class Meter:
    num: int
    den: int
    beats_per_bar: int  # tracker beats per bar
    beat_ticks: int  # ticks per tracker beat
    phase: int  # a beat index (in BeatMap indexing) that is a downbeat, mod beats_per_bar
    score: float  # agreement with tracker downbeats (0..1)


def from_time_sig(ts: str) -> tuple[int, int, int, int]:
    """'6/8' → (num, den, beats_per_bar, beat_ticks). Compound x/8 meters beat in dotted quarters."""
    num, den = (int(x) for x in ts.split("/"))
    if den == 8 and num % 3 == 0 and num > 3:
        return num, den, num // 3, PPQ * 3 // 2
    return num, den, num, PPQ * 4 // den


def _agreement(downbeats: list[int], bpb: int, phase: int, n_beats: int) -> float:
    if not downbeats:
        return 0.0
    lo, hi = min(downbeats), max(downbeats)
    predicted = [k for k in range(lo, hi + 1) if (k - phase) % bpb == 0]
    hits = sum(1 for d in downbeats if (d - phase) % bpb == 0)
    return 2 * hits / (len(predicted) + len(downbeats))


def _best_phase(downbeats: list[int], bpb: int, n_beats: int) -> tuple[int, float]:
    return max(((p, _agreement(downbeats, bpb, p, n_beats)) for p in range(bpb)), key=lambda x: x[1])


# small preference for the most common meters when agreement is ambiguous
PRIOR_BONUS = {4: 0.03, 3: 0.0, 2: -0.02}


def infer_meter(downbeats: list[int], n_beats: int, time_sig: str | None, compound: bool) -> Meter:
    """compound: whether tracker beats are dotted quarters (decided by the rhythm fit)."""
    if time_sig:
        num, den, bpb, bt = from_time_sig(time_sig)
        phase, score = _best_phase(downbeats, bpb, n_beats)
        return Meter(num, den, bpb, bt, phase, score)

    best = None
    for bpb in (2, 3, 4):
        phase, score = _best_phase(downbeats, bpb, n_beats)
        cand = (score + PRIOR_BONUS[bpb], bpb, phase, score)
        if best is None or cand[0] > best[0]:
            best = cand
    _, bpb, phase, score = best
    if score < 0.3:  # tracker downbeats are noise: default to 4 beats anchored on the first one
        bpb, phase = 4, (downbeats[0] % 4 if downbeats else 0)
    if compound:  # e.g. 2 dotted-quarter beats → 6/8
        return Meter(bpb * 3, 8, bpb, PPQ * 3 // 2, phase, score)
    return Meter(bpb, 4, bpb, PPQ, phase, score)
