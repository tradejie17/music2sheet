"""Content-addressed stage cache.

key = sha256(stage, stage_version, canonical params, upstream keys). Each entry is a
directory data/cache/<stage>/<key>/ holding `artifact.json` (+ optional extra files)
and `meta.json`. Writes are atomic: build in a tmp dir, then rename.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def canonical(obj: Any) -> str:
    if isinstance(obj, BaseModel):
        obj = obj.model_dump(mode="json")
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def stage_key(stage: str, version: int, params: Any, upstream: list[str]) -> str:
    h = hashlib.sha256()
    for part in (stage, str(version), canonical(params), *upstream):
        h.update(part.encode())
        h.update(b"\0")
    return h.hexdigest()[:24]


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


class StageCache:
    def __init__(self, root: Path):
        self.root = root

    def entry(self, stage: str, key: str) -> Path:
        return self.root / stage / key

    def has(self, stage: str, key: str) -> bool:
        return (self.entry(stage, key) / "meta.json").exists()

    def get_or_build(self, stage: str, key: str, build: Callable[[Path], None]) -> tuple[Path, bool]:
        """Return (entry_dir, hit). `build(tmp_dir)` must write artifact files into tmp_dir."""
        final = self.entry(stage, key)
        if (final / "meta.json").exists():
            return final, True
        tmp = self.root / stage / f".tmp-{key}-{uuid.uuid4().hex[:8]}"
        tmp.mkdir(parents=True)
        try:
            t0 = time.perf_counter()
            build(tmp)
            (tmp / "meta.json").write_text(
                json.dumps({"stage": stage, "key": key, "built_s": round(time.perf_counter() - t0, 3)})
            )
            if final.exists():  # concurrent/partial leftover
                shutil.rmtree(final)
            tmp.rename(final)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        return final, False
