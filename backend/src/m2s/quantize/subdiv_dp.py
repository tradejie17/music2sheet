"""Per-beat subdivision choice by Viterbi over beats, then onset snapping.

For each beat k we pick a subdivision s. The cost balances timing error of the onsets
falling in that beat, notational complexity, and consistency with neighbouring beats
(switching between native and tuplet divisions is expensive).

What is "native" depends on the beat: a simple beat (quarter) divides natively in
2/4/8 with 3/6 as tuplets; a compound beat (dotted quarter) divides natively in
3/6/12 with 2/4 as duplets. The quantizer runs both models and keeps the cheaper one
(see quantizer.py), which is how simple vs compound meter is decided.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

import numpy as np

COLLISION = 3.0
UNREPRESENTATIVE = 1e6
ERR_CAP = 4.0
SAME_FAMILY = 0.4
CROSS_FAMILY = 1.5


@dataclass(frozen=True)
class BeatModel:
    name: str
    native: tuple[int, ...]  # finest-first order irrelevant; e.g. (2, 4, 8)
    tuplet: tuple[int, ...]
    complexity: dict[int, float]

    def family(self, s: int) -> str:
        return "neutral" if s == 1 else ("native" if s in self.native else "tuplet")

    def transition(self, a: int, b: int) -> float:
        if a == b:
            return 0.0
        fa, fb = self.family(a), self.family(b)
        if "neutral" in (fa, fb):
            return 0.0
        return SAME_FAMILY if fa == fb else CROSS_FAMILY

    def candidates(self, level: int, tuplets: bool) -> list[int]:
        """level = how many native subdivision steps the minimum grid allows (1..3)."""
        out = [1, *self.native[:level]]
        if tuplets:
            out += list(self.tuplet[: min(level, len(self.tuplet))])
        return out


# 16th sextuplets (6) cost 8: at a 1/16 grid they are the only division finer than 16ths,
# and cheaper settings turned fast runs of dense pop/anime piano into sextuplet brackets
# (Call of Silence: 114 → 26 triplet beats) while ASAP onset accuracy stays level
# (scratch sweep 2026-10-24: c6=4 median .933 mean .732, c6=8 median .932 mean .742).
SIMPLE = BeatModel("simple", (2, 4, 8), (3, 6), {1: 0.0, 2: 0.3, 4: 1.0, 8: 2.5, 3: 1.8, 6: 8.0})
COMPOUND = BeatModel("compound", (3, 6, 12), (2, 4), {1: 0.0, 3: 0.3, 6: 1.0, 12: 2.5, 2: 1.8, 4: 4.0})


@dataclass
class SubdivResult:
    subdiv: list[int]  # chosen s per beat index
    snapped: list[Fraction]  # snapped beat position per input onset
    cost: float  # total Viterbi cost (for model selection)


def choose_subdivisions(
    onset_pos: np.ndarray,
    ibi_of_beat,
    n_beats: int,
    cands: list[int],
    model: BeatModel = SIMPLE,
    tuplet_scale: float = 1.0,
    cluster_tol_s: float = 0.03,
) -> SubdivResult:
    """onset_pos: fractional beat positions (already clustered: one entry per distinct onset)."""
    eps = 1.0 / (2 * max(cands))
    owner = np.floor(onset_pos + eps).astype(int)
    owner = np.clip(owner, 0, n_beats - 1)
    per_beat: list[list[float]] = [[] for _ in range(n_beats)]
    for p, k in zip(onset_pos, owner):
        per_beat[k].append(float(p - k))  # fraction in [-eps, 1-eps)

    S = len(cands)
    cand_set = set(cands)
    first_tuplet = model.tuplet[0]
    cplx = np.array([model.complexity[s] * (tuplet_scale if s == first_tuplet else 1.0) for s in cands])
    local = np.zeros((n_beats, S))
    for k in range(n_beats):
        fr = per_beat[k]
        if not fr:
            local[k] = cplx
            continue
        ibi = ibi_of_beat(k)
        tol = float(np.clip(0.08 * ibi, 0.025, 0.05))  # seconds
        fr_arr = np.asarray(fr)
        for j, s in enumerate(cands):
            grid_idx = np.clip(np.round(fr_arr * s), 0, s)
            # a grid finer than the onsets need (e.g. 6 for positions {0, 3/6}) must not
            # stand in for the coarser one: that one has its own family and complexity
            g = np.gcd.reduce(np.append(grid_idx.astype(int) % s, s))
            if g > 1 and s // g in cand_set:
                local[k, j] = UNREPRESENTATIVE
                continue
            err_s = np.abs(fr_arr - grid_idx / s) * ibi
            e = np.minimum((err_s / tol) ** 2, ERR_CAP).sum()
            local[k, j] = e + _collision_cost(fr_arr, grid_idx, ibi, cluster_tol_s) + cplx[j]

    trans = np.array([[model.transition(a, b) for b in cands] for a in cands])
    cost = local[0].copy()
    back = np.zeros((n_beats, S), dtype=int)
    for k in range(1, n_beats):
        tot = cost[:, None] + trans
        back[k] = np.argmin(tot, axis=0)
        cost = tot[back[k], np.arange(S)] + local[k]
    total = float(np.min(cost))
    path = [int(np.argmin(cost))]
    for k in range(n_beats - 1, 0, -1):
        path.append(int(back[k][path[-1]]))
    path.reverse()
    subdiv = [cands[j] for j in path]

    snapped = []
    for p, k in zip(onset_pos, owner):
        s = subdiv[k]
        j = int(np.clip(round((p - k) * s), 0, s))
        snapped.append(Fraction(int(k)) + Fraction(j, s))
    return SubdivResult(subdiv=subdiv, snapped=snapped, cost=total)


def _collision_cost(fr: np.ndarray, grid_idx: np.ndarray, ibi: float, tol: float) -> float:
    """Penalty for distinct onsets merged onto one grid point, scaled by how far apart they
    were: a chord split by jitter (gap ≈ tol) costs ~0, two real notes cost COLLISION."""
    order = np.argsort(fr)
    f, g = fr[order], grid_idx[order]
    gaps_s = np.diff(f)[np.diff(g) == 0] * ibi
    return float(COLLISION * np.clip((gaps_s - tol) / tol, 0.0, 1.0).sum())


def snap_to_beat_grid(pos: float, subdiv: list[int], floor_s: int = 1) -> Fraction:
    """Snap an arbitrary beat position (e.g. an offset) to the grid of the beat it lands in."""
    k = int(np.clip(np.floor(pos), 0, len(subdiv) - 1))
    s = max(subdiv[k], floor_s)
    j = int(round((pos - k) * s))
    return Fraction(k) + Fraction(j, s)
