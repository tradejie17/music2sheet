"""ScoreDoc: the editable single source of truth.

ScoreDoc is *logical*, not graphical: one ScoreNote is one sounding note with its
full duration. Barline splits, ties across barlines, rests, beams and tuplet
brackets are derived when rendering MusicXML, so edits never touch them.

Time is integer ticks at PPQ=960 (divisible by 3 for triplets/sextuplets).
Tick 0 is the start of measure 1, which is `pickup_ticks` long when > 0.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from m2s.models.artifacts import PedalKind, TempoMap

PPQ = 960

Lockable = Literal["pitch", "time", "staff", "voice", "spelling"]


class Spelling(BaseModel):
    step: Literal["C", "D", "E", "F", "G", "A", "B"]
    alter: int = Field(0, ge=-2, le=2)
    octave: int


class ScoreNote(BaseModel):
    id: str
    pitch: int = Field(ge=0, le=127)
    start: int = Field(ge=0)
    dur: int = Field(gt=0)
    staff: Literal[1, 2] = 1  # 1 = treble/RH, 2 = bass/LH
    voice: int = Field(1, ge=1, le=4)
    spelling: Spelling | None = None  # None = auto
    tie_next: bool = False  # explicit tie to the next same-pitch note (beyond barline ties)
    velocity: int = Field(64, ge=0, le=127)
    confidence: float = 1.0
    src_ids: list[str] = []
    locked: set[Lockable] = set()


class ScorePedal(BaseModel):
    id: str
    kind: PedalKind = "sustain"
    start: int = Field(ge=0)
    end: int
    locked: bool = False


class MeterChange(BaseModel):
    tick: int
    num: int
    den: int


class KeyChange(BaseModel):
    tick: int
    fifths: int = Field(ge=-7, le=7)
    mode: Literal["major", "minor"] = "major"


class TempoMark(BaseModel):
    tick: int
    bpm: float
    text: str | None = None


class ClefChange(BaseModel):
    tick: int
    staff: Literal[1, 2]
    clef: Literal["treble", "bass", "treble8vb", "bass8va"]


class Engraving(BaseModel):
    """How the doc is printed; never changes what sounds (playback and MIDI use the notes
    and pedals as they are)."""

    pedal_marks: bool = True  # False: one "con Ped." at the start instead of every Ped./*
    ottava: bool = False  # 8va / 8vb lines over long passages far above/below the staves
    # readable value grouping: 3½ beats from a beat → 3 + ½ (not 1 + 1 + 1½); a triplet
    # group repeating the one before prints without bracket and "3"
    tidy: bool = False


class Provenance(BaseModel):
    audio_sha: str | None = None
    stage_keys: dict[str, str] = {}
    versions: dict[str, str] = {}


class ScoreDoc(BaseModel):
    schema_version: int = 1
    revision: int = 0
    ppq: Literal[960] = PPQ
    title: str = "Untitled"
    pickup_ticks: int = Field(0, ge=0)
    meters: list[MeterChange]
    keys: list[KeyChange]
    tempos: list[TempoMark] = []
    clefs: list[ClefChange] = []
    notes: list[ScoreNote] = []
    pedals: list[ScorePedal] = []
    tempo_map: TempoMap | None = None
    engraving: Engraving = Engraving()
    provenance: Provenance = Provenance()

    @model_validator(mode="after")
    def _check(self) -> ScoreDoc:
        if not self.meters or self.meters[0].tick != 0:
            raise ValueError("meters must start at tick 0")
        if not self.keys or self.keys[0].tick != 0:
            raise ValueError("keys must start at tick 0")
        ids = [n.id for n in self.notes]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate note ids")
        for m in self.meters[1:]:
            if m.tick not in self.barline_ticks(m.tick + 1):
                raise ValueError(f"meter change at tick {m.tick} is not on a barline")
        return self

    def measure_len(self, m: MeterChange) -> int:
        return self.ppq * 4 * m.num // m.den

    def barline_ticks(self, until: int) -> list[int]:
        """Ticks where each measure starts, up to (and including the first >=) `until`."""
        out = [0]
        t = self.pickup_ticks if self.pickup_ticks else self.measure_len(self.meters[0])
        meters = sorted(self.meters, key=lambda m: m.tick)
        while out[-1] < until:
            out.append(t)
            cur = next(m for m in reversed(meters) if m.tick <= t)
            t += self.measure_len(cur)
        return out
