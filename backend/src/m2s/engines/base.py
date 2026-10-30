"""ML engine protocol.

Engines run inside a short-lived subprocess (see runner.py) so model memory is
released when the stage finishes and a crash can't take down the worker.
Progress is reported as JSON lines on stdout: {"progress": 0.4, "message": "..."}.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Protocol


def report(progress: float, message: str = "") -> None:
    print(json.dumps({"progress": round(progress, 3), "message": message}), flush=True)


class Engine(Protocol):
    name: str
    version: str

    def run(self, audio: Path, out_dir: Path, device: str) -> None:
        """Read `audio` (44.1k mono wav), write `artifact.json` into out_dir."""


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
