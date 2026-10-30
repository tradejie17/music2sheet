"""Per-job files: data/jobs/<id>/{source.*, audio.wav, transcription.json, beats.json,
doc_rev_<N>.json, render/<doc-hash>/{score.musicxml, idmap.json, score.pdf}}."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from m2s.models.scoredoc import ScoreDoc

# bump when notation/rendering code changes so cached renders are rebuilt
RENDER_VERSION = 4


class JobStore:
    def __init__(self, root: Path):
        self.root = root

    def dir(self, job_id: str) -> Path:
        d = self.root / job_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def source(self, job_id: str) -> Path | None:
        return next(iter(sorted(self.dir(job_id).glob("source.*"))), None)

    def audio(self, job_id: str) -> Path:
        return self.dir(job_id) / "audio.wav"

    def doc_path(self, job_id: str, rev: int) -> Path:
        return self.dir(job_id) / f"doc_rev_{rev}.json"

    def save_doc(self, job_id: str, doc: ScoreDoc) -> None:
        path = self.doc_path(job_id, doc.revision)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(doc.model_dump_json())
        os.replace(tmp, path)

    def load_doc(self, job_id: str, rev: int) -> ScoreDoc:
        return ScoreDoc.model_validate_json(self.doc_path(job_id, rev).read_text())

    @staticmethod
    def doc_hash(doc: ScoreDoc) -> str:
        # revision/provenance don't change the notation
        body = doc.model_dump(mode="json", exclude={"revision", "provenance"})
        payload = json.dumps({"v": RENDER_VERSION, "doc": body}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:20]

    def render_dir(self, job_id: str, doc: ScoreDoc) -> Path:
        d = self.dir(job_id) / "render" / self.doc_hash(doc)
        d.mkdir(parents=True, exist_ok=True)
        return d
