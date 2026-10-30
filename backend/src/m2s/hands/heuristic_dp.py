"""Two-hand (staff) separation by Viterbi over onset clusters.

Each cluster of notes sharing a start tick is split into "lowest j notes → LH, rest
→ RH". States are the n+1 possible splits. Costs:
  * infeasible: a hand spanning > MAX_SPAN semitones or holding > 5 notes
  * soft register zones (LH above 67 / RH below 53), per semitone
  * continuity: distance from each hand's smoothed centroid (carried per state)
  * crossing: a hand landing on the other side of the other hand's centroid
Notes whose staff the user locked keep their staff.
"""

from __future__ import annotations

import math
from itertools import groupby

from m2s.models.scoredoc import ScoreNote

INF = math.inf
LH_SOFT_MAX, RH_SOFT_MIN = 67, 53
ZONE_W = 0.15
CONT_DIV = 6.0
CROSS = 2.0
ALPHA = 0.3
MAX_NOTES = 5
START_CENTROIDS = (48.0, 72.0)


def _mean(xs: list[int]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _local(lh: list[int], rh: list[int], max_span: int) -> float:
    for h in (lh, rh):
        if len(h) > MAX_NOTES or (h and h[-1] - h[0] > max_span):
            return INF
    cost = sum(max(0, p - LH_SOFT_MAX) for p in lh) * ZONE_W
    cost += sum(max(0, RH_SOFT_MIN - p) for p in rh) * ZONE_W
    return cost


def _trans(lh: list[int], rh: list[int], cl: float, cr: float) -> float:
    cost = 0.0
    ml, mr = _mean(lh), _mean(rh)
    if ml is not None:
        cost += abs(ml - cl) / CONT_DIV + (CROSS if ml > cr else 0.0)
    if mr is not None:
        cost += abs(mr - cr) / CONT_DIV + (CROSS if mr < cl else 0.0)
    return cost


def _smooth(c: float, xs: list[int]) -> float:
    m = _mean(xs)
    return c if m is None else (1 - ALPHA) * c + ALPHA * m


def separate_hands(notes: list[ScoreNote], style: str = "classical") -> None:
    """Assign `staff` in place (1 = RH/treble, 2 = LH/bass)."""
    max_span = 14 if style == "classical" else 12
    free = [n for n in notes if "staff" not in n.locked]
    clusters = [
        sorted(g, key=lambda n: n.pitch)
        for _, g in groupby(sorted(free, key=lambda n: n.start), key=lambda n: n.start)
    ]
    if not clusters:
        return

    # Viterbi with per-state carried centroids (first-order approximation)
    prev: list[tuple[float, float, float]] = [(0.0, *START_CENTROIDS)]  # (cost, cl, cr)
    backs: list[list[int]] = []
    for cl_notes in clusters:
        pitches = [n.pitch for n in cl_notes]
        n = len(pitches)
        cur: list[tuple[float, float, float]] = []
        back: list[int] = []
        locals_ = [_local(pitches[:j], pitches[j:], max_span) for j in range(n + 1)]
        if all(c == INF for c in locals_):  # nothing playable: split at the largest gap
            gaps = [pitches[i + 1] - pitches[i] for i in range(n - 1)]
            jbest = gaps.index(max(gaps)) + 1 if gaps else 0
            locals_ = [0.0 if j == jbest else INF for j in range(n + 1)]
        for j in range(n + 1):
            lh, rh = pitches[:j], pitches[j:]
            best, arg = INF, 0
            for i, (pc, cl, cr) in enumerate(prev):
                c = pc + _trans(lh, rh, cl, cr)
                if c < best:
                    best, arg = c, i
            _, cl, cr = prev[arg]
            cur.append((best + locals_[j], _smooth(cl, lh), _smooth(cr, rh)))
            back.append(arg)
        backs.append(back)
        prev = cur

    j = min(range(len(prev)), key=lambda i: prev[i][0])
    for k in range(len(clusters) - 1, -1, -1):
        for idx, note in enumerate(clusters[k]):
            note.staff = 2 if idx < j else 1
        j = backs[k][j]
