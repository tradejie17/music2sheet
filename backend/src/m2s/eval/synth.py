"""Synthetic 'performances' of known scores, for quantizer/hand-separation tests.

A score is a list of (pitch, start_beats, dur_beats) in *tracker beats*. We render it
with a smoothly drifting tempo plus Gaussian onset jitter, and produce the beat grid a
tracker would report (with optional jitter / dropped downbeats).
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

import numpy as np

from m2s.models.artifacts import BeatGrid, NoteEvent, Transcription

ScoreItem = tuple[int, Fraction | float, Fraction | float]


@dataclass
class Performance:
    transcription: Transcription
    beats: BeatGrid
    beat_times: np.ndarray


def make_performance(
    score: list[ScoreItem],
    bpm: float = 100.0,
    drift: float = 0.10,
    jitter_s: float = 0.015,
    beat_jitter_s: float = 0.008,
    beats_per_bar: int = 4,
    first_downbeat: int = 0,
    downbeat_dropout: float = 0.0,
    lead_in_s: float = 1.0,
    seed: int = 0,
) -> Performance:
    rng = np.random.default_rng(seed)
    n_beats = int(max(float(s) + float(d) for _, s, d in score)) + 3
    # tempo curve: sinusoidal drift of ±drift around bpm
    phase = rng.uniform(0, 2 * np.pi)
    k = np.arange(n_beats + 1)
    local_bpm = bpm * (1 + drift * np.sin(2 * np.pi * k / max(n_beats, 16) + phase))
    beat_times = lead_in_s + np.concatenate([[0.0], np.cumsum(60.0 / local_bpm[:-1])])

    def t_of(pos: float) -> float:
        return float(np.interp(pos, k, beat_times))

    notes = []
    for i, (pitch, s, d) in enumerate(sorted(score, key=lambda x: (float(x[1]), x[0]))):
        on = t_of(float(s)) + rng.normal(0, jitter_s)
        off = t_of(float(s) + float(d)) - 0.02 + rng.normal(0, jitter_s)
        notes.append(
            NoteEvent(
                id=f"e{i:05d}",
                pitch=pitch,
                onset_s=on,
                offset_s=max(off, on + 0.03),
                velocity=int(rng.integers(50, 90)),
                engine="synth",
            )
        )
    tracked = beat_times + rng.normal(0, beat_jitter_s, len(beat_times))
    down = [
        float(tracked[i])
        for i in range(first_downbeat, len(tracked), beats_per_bar)
        if rng.uniform() >= downbeat_dropout
    ]
    tr = Transcription(engine="synth", engine_version="0", duration_s=float(beat_times[-1]) + 1, notes=notes)
    grid = BeatGrid(beats_s=tracked.tolist(), downbeats_s=down, source="synth")
    return Performance(transcription=tr, beats=grid, beat_times=beat_times)


def F(x: str | int) -> Fraction:
    return Fraction(x)
