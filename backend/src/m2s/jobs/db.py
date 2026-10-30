"""SQLite job table (WAL). One row per transcription job; the worker claims queued
rows atomically with UPDATE … RETURNING."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source TEXT NOT NULL,            -- original filename or URL
    status TEXT NOT NULL,            -- queued | running | done | error | cancelled
    stage TEXT NOT NULL DEFAULT '',
    progress REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    params_json TEXT NOT NULL,       -- Priors
    audio_sha TEXT,
    duration_s REAL,
    doc_rev INTEGER,                 -- latest ScoreDoc revision on disk
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""

COLUMNS = [
    "id", "title", "source", "status", "stage", "progress", "message", "params_json",
    "audio_sha", "duration_s", "doc_rev", "error", "created_at", "updated_at",
]  # fmt: skip


class JobDB:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()

    @staticmethod
    def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
        if r is None:
            return None
        d = dict(r)
        d["params"] = json.loads(d.pop("params_json"))
        return d

    def create(self, title: str, source: str, params: dict) -> dict:
        now = time.time()
        job_id = uuid.uuid4().hex[:12]
        with self.conn() as c:
            c.execute(
                "INSERT INTO jobs (id, title, source, status, params_json, created_at, updated_at)"
                " VALUES (?, ?, ?, 'queued', ?, ?, ?)",
                (job_id, title, source, json.dumps(params), now, now),
            )
        return self.get(job_id)

    def get(self, job_id: str) -> dict | None:
        with self.conn() as c:
            return self._row(c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())

    def list(self, limit: int = 100) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def update(self, job_id: str, **fields) -> None:
        if "params" in fields:
            fields["params_json"] = json.dumps(fields.pop("params"))
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))

    def claim(self) -> dict | None:
        with self.conn() as c:
            r = c.execute(
                "UPDATE jobs SET status = 'running', stage = 'starting', updated_at = ?"
                " WHERE id = (SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1)"
                " RETURNING *",
                (time.time(),),
            ).fetchone()
        return self._row(r)

    def requeue_stale(self) -> None:
        """Jobs left 'running' by a crashed worker go back to the queue."""
        with self.conn() as c:
            c.execute("UPDATE jobs SET status = 'queued', stage = '', progress = 0 WHERE status = 'running'")

    def delete(self, job_id: str) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
