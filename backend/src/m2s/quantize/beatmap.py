"""Beat grid cleanup and seconds ↔ beat-position mapping (stage S3).

Beat position: pos(t) = i + (t - b_i) / (b_{i+1} - b_i), piecewise-linear over the
cleaned beat list, extended past both ends with the median edge inter-beat interval
so every note has a position.
"""

from __future__ import annotations

import numpy as np

from m2s.models.artifacts import BeatGrid

MIN_BEATS = 8
FALLBACK_BPM = 100.0


def _local_median_ibi(beats: np.ndarray, i: int, w: int = 8) -> float:
    ibi = np.diff(beats)
    lo, hi = max(0, i - w), min(len(ibi), i + w)
    return float(np.median(ibi[lo:hi]))


def repair_gaps(beats: np.ndarray, downbeats: np.ndarray) -> np.ndarray:
    """Fill ~2x/3x gaps (missed beats) and drop ~0.5x gaps (spurious beats)."""
    if len(beats) < 3:
        return beats
    out = [beats[0]]
    for i in range(1, len(beats)):
        med = _local_median_ibi(beats, i - 1)
        gap = beats[i] - out[-1]
        if gap > 1.6 * med:
            n = int(round(gap / med))
            last = out[-1]  # a lazy generator would re-read out[-1] after each append
            out.extend([last + gap * k / n for k in range(1, n)])
            out.append(beats[i])
        elif gap < 0.55 * med:
            # spurious: keep whichever of the two is closer to a downbeat (else the earlier one)
            if len(downbeats) and np.min(np.abs(downbeats - beats[i])) < np.min(np.abs(downbeats - out[-1])):
                out[-1] = beats[i]
        else:
            out.append(beats[i])
    return np.asarray(out)


def apply_bpm_prior(beats: np.ndarray, downbeats: np.ndarray, bpm: float | None) -> np.ndarray:
    """Fix beat-octave errors (tracker at 2x or 0.5x the user's tempo)."""
    if bpm is None or len(beats) < 4:
        return beats
    detected = 60.0 / float(np.median(np.diff(beats)))
    r = detected / bpm
    if 1.6 < r < 2.5:  # tracker too fast: keep every other beat, choose phase hitting downbeats
        best = max(
            (beats[p::2] for p in (0, 1)),
            key=lambda b: (
                np.sum(np.min(np.abs(downbeats[:, None] - b[None, :]), axis=1) < 0.07)
                if len(downbeats)
                else 0
            ),
        )
        return best
    if 0.4 < r < 0.625:  # tracker too slow: insert midpoints
        mids = (beats[:-1] + beats[1:]) / 2
        return np.sort(np.concatenate([beats, mids]))
    return beats


def clean_beats(grid: BeatGrid, bpm: float | None, first_onset: float, last_onset: float) -> np.ndarray:
    beats = np.asarray(grid.beats_s, dtype=float)
    downbeats = np.asarray(grid.downbeats_s, dtype=float)
    if len(beats) < MIN_BEATS:
        ibi = 60.0 / (bpm or FALLBACK_BPM)
        return np.arange(first_onset, last_onset + ibi, ibi)
    beats = apply_bpm_prior(beats, downbeats, bpm)
    return repair_gaps(beats, downbeats)


class BeatMap:
    """Bidirectional seconds ↔ fractional beat index over an extended beat list.

    Beats are extended so index 0 is at or before `t_min` and the last beat is
    after `t_max`; `offset` records how many beats were prepended.
    """

    def __init__(self, beats: np.ndarray, t_min: float, t_max: float):
        beats = np.asarray(beats, dtype=float)
        edge = min(4, len(beats) - 1)
        ibi_head = float(np.median(np.diff(beats[: edge + 1])))
        ibi_tail = float(np.median(np.diff(beats[-edge - 1 :])))
        pre = []
        t = beats[0]
        while t > t_min - 1e-9:
            t -= ibi_head
            pre.append(t)
        post = []
        t = beats[-1]
        while t <= t_max + ibi_tail:
            t += ibi_tail
            post.append(t)
        self.offset = len(pre)
        self.beats = np.concatenate([pre[::-1], beats, post])
        self.idx = np.arange(len(self.beats), dtype=float)

    def pos(self, t: float | np.ndarray) -> np.ndarray:
        return np.interp(t, self.beats, self.idx)

    def time(self, pos: float | np.ndarray) -> np.ndarray:
        return np.interp(pos, self.idx, self.beats)

    def ibi(self, k: int) -> float:
        k = int(np.clip(k, 0, len(self.beats) - 2))
        return float(self.beats[k + 1] - self.beats[k])

    def downbeat_indices(self, downbeats_s: list[float], tol: float = 0.25) -> list[int]:
        out = []
        for d in downbeats_s:
            p = float(self.pos(d))
            k = int(round(p))
            if abs(p - k) <= tol:
                out.append(k)
        return sorted(set(out))
