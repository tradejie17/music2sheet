"""Intermediate pipeline artifacts (everything before the editable ScoreDoc).

Times here are in seconds of the source audio. Quantized/musical time lives in
ScoreDoc as integer ticks (see scoredoc.PPQ).
"""

from __future__ import annotations

import bisect
from typing import Literal

from pydantic import BaseModel, Field

PedalKind = Literal["sustain", "soft", "sostenuto"]


class NoteEvent(BaseModel):
    id: str
    pitch: int = Field(ge=0, le=127)
    onset_s: float
    offset_s: float
    velocity: int = Field(ge=0, le=127)
    confidence: float | None = None
    engine: str


class PedalEvent(BaseModel):
    id: str
    kind: PedalKind
    start_s: float
    end_s: float


class Transcription(BaseModel):
    engine: str
    engine_version: str
    duration_s: float
    notes: list[NoteEvent]
    pedals: list[PedalEvent] = []


class BeatGrid(BaseModel):
    beats_s: list[float]
    downbeats_s: list[float]
    source: str
    # frame-wise tracker logits (npz with `beat`, `downbeat`, `fps`). In the cached
    # artifact this is a file name relative to the artifact; s2_beats resolves it to an
    # absolute path. None (old cache entries, synthetic grids) → S3 uses the legacy
    # peak-picked beats + clean_beats path.
    activations: str | None = None


class TempoMap(BaseModel):
    """Monotonic (tick, seconds) anchors, one per beat; piecewise-linear both ways.

    Outside the anchor range we extrapolate with the first/last segment slope.
    """

    anchors: list[tuple[int, float]]

    def _seg(self, xs: list[float], x: float) -> int:
        i = bisect.bisect_right(xs, x) - 1
        return min(max(i, 0), len(xs) - 2)

    def tick_to_sec(self, tick: float) -> float:
        ticks = [a[0] for a in self.anchors]
        i = self._seg(ticks, tick)
        (t0, s0), (t1, s1) = self.anchors[i], self.anchors[i + 1]
        return s0 + (tick - t0) * (s1 - s0) / (t1 - t0)

    def sec_to_tick(self, sec: float) -> float:
        secs = [a[1] for a in self.anchors]
        i = self._seg(secs, sec)
        (t0, s0), (t1, s1) = self.anchors[i], self.anchors[i + 1]
        return t0 + (sec - s0) * (t1 - t0) / (s1 - s0)
