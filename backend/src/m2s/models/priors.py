"""Optional user-supplied priors (Klangio-style human-in-the-loop hints)."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

MinGrid = Literal["1/8", "1/16", "1/32"]
# 0 = faithful (everything transcribed), 1 = readable, 2 = simplified arrangement;
# see notation/simplify.py. The model default is 0 because jobs created before the
# option existed were generated that way; new jobs (UI, CLI, POST /api/jobs) default to 1.
SimplifyLevel = Literal[0, 1, 2]


class Priors(BaseModel):
    key: str | None = Field(None, description='e.g. "Eb major", "c# minor"; None = auto')
    bpm: float | None = Field(None, gt=20, lt=400)
    time_sig: str | None = Field(None, description='e.g. "3/4"; None = auto')
    min_grid: MinGrid = "1/16"
    triplets: bool = True
    style: Literal["pop", "classical"] = "classical"
    engine: Literal["transkun"] = "transkun"  # basic-pitch evaluated and deferred: docs/adr/0002
    simplify: SimplifyLevel = 0

    @field_validator("time_sig")
    @classmethod
    def _ts(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"\d{1,2}/(1|2|4|8|16)", v):
            raise ValueError("time_sig must look like 3/4 or 6/8")
        return v

    @field_validator("key")
    @classmethod
    def _key(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"[A-Ga-g][#b]?\s+(major|minor)", v.strip()):
            raise ValueError('key must look like "Eb major" or "c# minor"')
        return v
