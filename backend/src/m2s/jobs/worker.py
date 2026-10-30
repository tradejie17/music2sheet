"""Single worker process: claims queued jobs one at a time and runs the pipeline.

Concurrency is 1 on purpose (16 GB unified memory): ML stages run in their own
short-lived subprocesses, and only one exists at any moment.
"""

from __future__ import annotations

import logging
import time
import traceback

from m2s.config import settings
from m2s.ingest.audio import ingest
from m2s.ingest.youtube import download_audio
from m2s.jobs.db import JobDB
from m2s.jobs.store import JobStore
from m2s.models.priors import Priors
from m2s.pipeline.stages import Ctx, s1_transcribe, s2_beats, s3_s4_quantize, s5_s6_arrange
from m2s.render.midi_export import transcription_to_midi
from m2s.render.service import ensure_musicxml

log = logging.getLogger("m2s.worker")

# overall progress range per stage
STAGE_RANGE = {
    "download": (0.0, 0.05),
    "ingest": (0.05, 0.08),
    "transcribe": (0.08, 0.75),
    "beats": (0.75, 0.85),
    "quantize": (0.85, 0.9),
    "arrange": (0.9, 0.95),
    "render": (0.95, 1.0),
}


class Cancelled(Exception):
    pass


def run_job(db: JobDB, store: JobStore, job: dict) -> None:
    job_id = job["id"]
    cfg = settings()
    jd = store.dir(job_id)

    def progress(stage: str, frac: float, msg: str = "") -> None:
        if db.get(job_id)["status"] == "cancelled":
            raise Cancelled()
        lo, hi = STAGE_RANGE.get(stage, (0.0, 1.0))
        db.update(job_id, stage=stage, progress=round(lo + (hi - lo) * frac, 3), message=msg)

    priors = Priors.model_validate(job["params"])
    src = store.source(job_id)
    if src is None:
        url = job["source"]
        progress("download", 0.0, "downloading from YouTube")
        src, title = download_audio(url, jd, cfg.max_audio_s)
        db.update(job_id, title=title)
        job = {**job, "title": title}
    progress("ingest", 0.0, "decoding audio")
    wav, sha, dur = ingest(src, jd, cfg.max_audio_s)
    db.update(job_id, audio_sha=sha, duration_s=dur)

    ctx = Ctx(audio=wav, audio_sha=sha, priors=priors, progress=progress)
    tr = s1_transcribe(ctx)
    beats = s2_beats(ctx)
    (jd / "transcription.json").write_text(tr.model_dump_json())
    # a record of the tracker's beats; the activations stay in the stage cache (re-runs read
    # them through s2_beats), so no absolute cache path is written into the job directory
    (jd / "beats.json").write_text(beats.model_copy(update={"activations": None}).model_dump_json())
    transcription_to_midi(tr, jd / "raw.mid")
    doc, _ = s3_s4_quantize(ctx, tr, beats)
    doc = s5_s6_arrange(ctx, doc, title=job["title"])

    prev = job.get("doc_rev")
    doc.revision = 0 if prev is None else prev + 1
    progress("render", 0.0, "engraving")
    ensure_musicxml(store, job_id, doc)
    store.save_doc(job_id, doc)
    db.update(job_id, status="done", stage="done", progress=1.0, message="", doc_rev=doc.revision, error=None)


def main(poll_s: float = 0.5) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s worker %(levelname)s %(message)s")
    cfg = settings()
    db, store = JobDB(cfg.db_path), JobStore(cfg.jobs_dir)
    db.requeue_stale()
    log.info("worker ready (db=%s)", cfg.db_path)
    while True:
        job = db.claim()
        if job is None:
            time.sleep(poll_s)
            continue
        log.info("job %s: %s", job["id"], job["title"])
        t0 = time.perf_counter()
        try:
            run_job(db, store, job)
            log.info("job %s done in %.1fs", job["id"], time.perf_counter() - t0)
        except Cancelled:
            log.info("job %s cancelled", job["id"])
        except Exception as e:  # report every failure to the UI
            log.error("job %s failed: %s", job["id"], e)
            db.update(job["id"], status="error", error=f"{e}", message=traceback.format_exc()[-2000:])
