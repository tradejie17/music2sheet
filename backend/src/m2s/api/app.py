"""FastAPI app: jobs, ScoreDoc read/write, renders. Single local user, no auth."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from m2s.config import REPO_ROOT, settings
from m2s.jobs.db import JobDB
from m2s.jobs.store import JobStore
from m2s.models.priors import Priors
from m2s.models.scoredoc import ScoreDoc
from m2s.pipeline.rederive import Rederive, rederive
from m2s.render.pdf_mscore import RenderError
from m2s.render.service import doc_to_midi, ensure_musicxml, ensure_pdf

AUDIO_EXT = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".aiff", ".aif", ".webm", ".mp4"}


def _db() -> JobDB:
    return JobDB(settings().db_path)


def _store() -> JobStore:
    return JobStore(settings().jobs_dir)


@asynccontextmanager
async def lifespan(app: FastAPI):
    worker = None
    if getattr(app.state, "spawn_worker", False):
        worker = subprocess.Popen([sys.executable, "-m", "m2s.cli", "worker"])
    try:
        yield
    finally:
        if worker is not None:
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()


app = FastAPI(title="music2sheet", lifespan=lifespan)


def _job(job_id: str) -> dict:
    job = _db().get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job


def _doc(job_id: str, rev: int | None = None) -> ScoreDoc:
    job = _job(job_id)
    if job["doc_rev"] is None:
        raise HTTPException(409, "score not ready")
    return _store().load_doc(job_id, job["doc_rev"] if rev is None else rev)


@app.get("/api/health")
def health() -> dict:
    cfg = settings()
    return {
        "mscore": cfg.mscore_path.exists(),
        "deno": shutil.which("deno") is not None,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "transkun_device": cfg.transkun_device,
    }


# Priors.simplify defaults to 0 so that jobs stored before the option existed keep
# reading as faithful; jobs created now default to the readable score.
NEW_JOB_SIMPLIFY = 1


@app.post("/api/jobs")
async def create_job(
    file: Annotated[UploadFile | None, File()] = None,
    youtube_url: Annotated[str | None, Form()] = None,
    priors: Annotated[str, Form()] = "{}",
) -> dict:
    try:
        p = Priors.model_validate_json(priors)
    except ValidationError as e:
        raise HTTPException(422, json.loads(e.json())) from e
    if "simplify" not in p.model_fields_set:  # new jobs: readable score (docs/adr/0007-simplify.md)
        p = p.model_copy(update={"simplify": NEW_JOB_SIMPLIFY})
    if (file is None) == (not youtube_url):
        raise HTTPException(422, "provide exactly one of: file, youtube_url")
    db, store = _db(), _store()
    if file is not None:
        ext = Path(file.filename or "").suffix.lower()
        if ext not in AUDIO_EXT:
            raise HTTPException(422, f"unsupported file type {ext or '(none)'}")
        title = Path(file.filename).stem
        job = db.create(title=title, source=file.filename, params=p.model_dump())
        with (store.dir(job["id"]) / f"source{ext}").open("wb") as out:
            shutil.copyfileobj(file.file, out)
    else:
        job = db.create(title="YouTube audio", source=youtube_url.strip(), params=p.model_dump())
    return job


@app.get("/api/jobs")
def list_jobs() -> list[dict]:
    return _db().list()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    return _job(job_id)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    _job(job_id)
    _db().delete(job_id)
    shutil.rmtree(settings().jobs_dir / job_id, ignore_errors=True)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    job = _job(job_id)
    if job["status"] in ("queued", "running"):
        _db().update(job_id, status="cancelled", message="cancelled by user")
    return _job(job_id)


@app.post("/api/jobs/{job_id}/rerun")
def rerun_job(job_id: str, priors: Priors) -> dict:
    job = _job(job_id)
    if job["status"] in ("queued", "running"):
        raise HTTPException(409, "job is still running")
    _db().update(job_id, status="queued", params=priors.model_dump(), progress=0, stage="", error=None)
    return _job(job_id)


@app.get("/api/jobs/{job_id}/audio")
def get_audio(job_id: str) -> FileResponse:
    _job(job_id)
    path = _store().audio(job_id)
    if not path.exists():
        raise HTTPException(404, "audio not ready")
    return FileResponse(path, media_type="audio/wav")


@app.get("/api/jobs/{job_id}/transcription")
def get_transcription(job_id: str) -> Response:
    path = _store().dir(job_id) / "transcription.json"
    if not path.exists():
        raise HTTPException(404, "not ready")
    return Response(path.read_text(), media_type="application/json")


@app.get("/api/jobs/{job_id}/doc")
def get_doc(job_id: str, rev: int | None = None) -> ScoreDoc:
    return _doc(job_id, rev)


class DocPut(BaseModel):
    doc: ScoreDoc


@app.put("/api/jobs/{job_id}/doc")
def put_doc(job_id: str, body: DocPut) -> dict:
    """Save an edited doc. body.doc.revision must be the current revision (optimistic lock)."""
    job = _job(job_id)
    if body.doc.revision != job["doc_rev"]:
        return JSONResponse({"detail": "stale revision", "current": job["doc_rev"]}, status_code=409)
    doc = body.doc.model_copy(update={"revision": job["doc_rev"] + 1})
    # marks hand-edited docs, so the editor can warn before a rerun replaces them
    doc.provenance.versions["edited"] = "1"
    store = _store()
    store.save_doc(job_id, doc)
    _db().update(job_id, doc_rev=doc.revision)
    return {"revision": doc.revision, "doc_hash": store.doc_hash(doc)}


class RederiveBody(BaseModel):
    what: Rederive


@app.post("/api/jobs/{job_id}/doc/rederive")
def rederive_doc(job_id: str, body: RederiveBody) -> ScoreDoc:
    job = _job(job_id)
    use_svsep = settings().hand_model == "svsep"
    doc = rederive(_doc(job_id), body.what, Priors.model_validate(job["params"]).style, use_svsep)
    doc.revision = job["doc_rev"] + 1
    _store().save_doc(job_id, doc)
    _db().update(job_id, doc_rev=doc.revision)
    return doc


@app.get("/api/jobs/{job_id}/render/{kind}")
def get_render(job_id: str, kind: str, rev: int | None = None, download: bool = False) -> Response:
    job = _job(job_id)
    doc, store = _doc(job_id, rev), _store()
    safe_title = "".join(c if c.isalnum() or c in " -_" else "_" for c in job["title"]).strip() or "score"
    try:
        if kind == "musicxml":
            d = ensure_musicxml(store, job_id, doc)
            name = f"{safe_title}.musicxml" if download else None
            return FileResponse(
                d / "score.musicxml", media_type="application/vnd.recordare.musicxml+xml", filename=name
            )
        if kind == "idmap":
            d = ensure_musicxml(store, job_id, doc)
            return FileResponse(d / "idmap.json", media_type="application/json")
        if kind == "pdf":
            pdf = ensure_pdf(store, job_id, doc)
            return FileResponse(
                pdf, media_type="application/pdf", filename=f"{safe_title}.pdf" if download else None
            )
    except RenderError as e:
        raise HTTPException(500, str(e)) from e
    raise HTTPException(404, f"unknown render kind {kind}")


@app.get("/api/jobs/{job_id}/midi")
def get_midi(job_id: str, kind: str = "quantized", rev: int | None = None) -> FileResponse:
    job = _job(job_id)
    store = _store()
    if kind == "raw":
        path = store.dir(job_id) / "raw.mid"
        if not path.exists():
            raise HTTPException(404, "not ready")
        return FileResponse(path, media_type="audio/midi", filename=f"{job['title']}-raw.mid")
    doc = _doc(job_id, rev)
    out = store.render_dir(job_id, doc) / "quantized.mid"
    if not out.exists():
        doc_to_midi(doc, out)
    return FileResponse(out, media_type="audio/midi", filename=f"{job['title']}.mid")


PIANO_UPSTREAM = "https://smpldsnds.github.io/sfzinstruments-splendid-grand-piano/samples"
_SAMPLE_NAME = re.compile(r"[A-Za-z]{2} [A-G]#?[0-8]\.(ogg|m4a)")


@app.get("/api/assets/piano/{name}")
def piano_sample(name: str) -> FileResponse:
    """Splendid Grand Piano samples (public domain) for in-browser score playback. Served
    from data/assets/piano/splendid; a missing file is fetched from upstream once, so the
    editor works offline after the first playback."""
    if not _SAMPLE_NAME.fullmatch(name):
        raise HTTPException(404, "unknown sample")
    path = settings().data_dir / "assets" / "piano" / "splendid" / name
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        try:
            with urllib.request.urlopen(f"{PIANO_UPSTREAM}/{urllib.parse.quote(name)}", timeout=30) as r:
                tmp.write_bytes(r.read())
        except Exception as e:  # offline and not cached yet
            raise HTTPException(404, f"sample not available offline: {e}") from e
        tmp.replace(path)
    media = "audio/ogg" if name.endswith(".ogg") else "audio/mp4"
    return FileResponse(
        path, media_type=media, headers={"Cache-Control": "public, max-age=31536000, immutable"}
    )


class SPAStaticFiles(StaticFiles):
    """Serve the built frontend; unknown paths (client-side routes) get index.html."""

    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as e:
            if e.status_code != 404 or path.startswith("api"):
                raise
            return await super().get_response("index.html", scope)


# Production: serve the built frontend (vite build → frontend/dist) from the same origin.
_dist = REPO_ROOT / "frontend" / "dist"
if _dist.exists():
    app.mount("/", SPAStaticFiles(directory=_dist, html=True), name="frontend")
