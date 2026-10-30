"""Pipeline stages S1..S7 with content-hash caching.

Each stage's cache key covers only the inputs it actually depends on, so e.g.
changing the time-signature prior re-runs S3 onward while transcription (S1)
and beat tracking (S2) are cache hits.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from m2s.config import Settings, settings
from m2s.engines.runner import EngineError, engine_version, run_engine
from m2s.models.artifacts import BeatGrid, Transcription
from m2s.models.priors import Priors
from m2s.pipeline.cache import StageCache, stage_key

# Bump a stage's version whenever its logic changes to invalidate cached outputs.
STAGE_VERSIONS = {
    "transcribe": 1,
    "beats": 2,  # 2: also saves frame activations (activations.npz) for the S3 beat DP
}

ProgressFn = Callable[[str, float, str], None]  # (stage, fraction 0..1 within stage, message)


@dataclass
class Ctx:
    audio: Path
    audio_sha: str
    priors: Priors
    cfg: Settings = field(default_factory=settings)
    progress: ProgressFn | None = None
    keys: dict[str, str] = field(default_factory=dict)  # stage -> cache key (provenance)
    hits: dict[str, bool] = field(default_factory=dict)
    # performed (onset, offset) per transcription note id, set by S4 for S6's simplify
    perf_s: dict[str, tuple[float, float]] | None = None

    @property
    def cache(self) -> StageCache:
        return StageCache(self.cfg.cache_dir)

    def report(self, stage: str, frac: float, msg: str = "") -> None:
        if self.progress:
            self.progress(stage, frac, msg)


def _engine_stage(ctx: Ctx, stage: str, engine: str, device: str, upstream: list[str]) -> Path:
    key = stage_key(stage, STAGE_VERSIONS[stage], {"engine": engine, "v": engine_version(engine)}, upstream)
    ctx.report(stage, 0.0, "starting")

    def build(tmp: Path) -> None:
        def on_progress(frac: float, msg: str) -> None:
            ctx.report(stage, frac, msg)

        try:
            run_engine(engine, ctx.audio, tmp, device, on_progress, ctx.cfg.engine_rss_limit_gb)
        except EngineError:
            if device == "cpu":
                raise
            ctx.report(stage, 0.05, f"{device} failed, retrying on cpu")
            run_engine(engine, ctx.audio, tmp, "cpu", on_progress, ctx.cfg.engine_rss_limit_gb)

    entry, hit = ctx.cache.get_or_build(stage, key, build)
    ctx.keys[stage], ctx.hits[stage] = key, hit
    ctx.report(stage, 1.0, "cached" if hit else "done")
    return entry / "artifact.json"


def s1_transcribe(ctx: Ctx) -> Transcription:
    engine = ctx.priors.engine
    if engine != "transkun":
        raise NotImplementedError(f"engine {engine} not available yet")
    path = _engine_stage(ctx, "transcribe", engine, ctx.cfg.transkun_device, [ctx.audio_sha])
    return Transcription.model_validate_json(path.read_text())


def s2_beats(ctx: Ctx) -> BeatGrid:
    path = _engine_stage(ctx, "beats", "beat_this", ctx.cfg.beat_device, [ctx.audio_sha])
    grid = BeatGrid.model_validate_json(path.read_text())
    if grid.activations:  # stored relative to the cache entry
        grid.activations = str(path.parent / grid.activations)
    return grid


@dataclass
class S3Beats:
    beats: np.ndarray  # seconds, one tempo level throughout
    downbeats: list[float]
    method: str  # "dp" (tempo-continuity DP over activations) | "legacy" (peaks + clean_beats)
    bpm: float | None = None  # tracked tempo at the beat level
    confidence: float | None = None  # tempo-level confidence (dp, automatic level only)
    from_prior: bool = False


def s3_beats(priors: Priors, tr: Transcription, grid: BeatGrid) -> S3Beats:
    """S3: one consistent beat level. DP over beat_this activations when the beats stage
    saved them; otherwise (old cache entries, synthetic grids) the legacy cleanup."""
    from m2s.quantize.beatdp import load_activations, track
    from m2s.quantize.beatmap import MIN_BEATS, clean_beats
    from m2s.quantize.meter import from_time_sig

    onsets = np.array([n.onset_s for n in tr.notes])
    last = max(n.offset_s for n in tr.notes)
    act = load_activations(grid.activations)
    if act is not None:
        bpb = from_time_sig(priors.time_sig)[2] if priors.time_sig else None
        r = track(act, onsets, float(onsets.min()), last, priors.bpm, bpb)
        if len(r.beats) >= MIN_BEATS:
            return S3Beats(r.beats, r.downbeats, "dp", r.bpm, r.confidence, r.from_prior)
    beats = clean_beats(grid, priors.bpm, float(onsets.min()), last)
    return S3Beats(beats, list(grid.downbeats_s), "legacy")


# The default 1/16 grid assumes a beat of ~0.4–0.7 s (sixteenths ≈ 100–180 ms). When the
# DP settles on a slow beat (ballads at ♩≈65, or after the user halves the tempo), the
# same music needs 32nds: at 65 bpm a 1/16 grid forces 130-ms runs into tuplets
# (Call of Silence: 33 tuplets/min, 9 % of notes > 60 ms off vs 0.2/min, 1.4 % with
# 1/32). A finer grid only helps when the timing is steady enough to resolve it, so it
# also requires a median beat-to-beat tempo change below STEADY_STEP_OCT (rubato
# classical pieces got fussier rhythms with 1/32: ADR 0006). Only the default grid is
# adapted; an explicit 1/8 or 1/32 is always honoured.
SLOW_BEAT_BPM = 80.0
STEADY_STEP_OCT = 0.05


def _grid_for_level(priors: Priors, b: S3Beats) -> Priors:
    if b.method != "dp" or not b.bpm or b.bpm >= SLOW_BEAT_BPM or priors.min_grid != "1/16":
        return priors
    steps = np.abs(np.diff(np.log2(np.diff(b.beats))))
    if len(steps) and float(np.median(steps)) < STEADY_STEP_OCT:
        return priors.model_copy(update={"min_grid": "1/32"})
    return priors


def s3_s4_quantize(ctx: Ctx, tr: Transcription, grid: BeatGrid):
    """S3 beat tracking/cleanup + S4 quantization. Pure CPU, < 1 s: not cached separately."""
    from m2s.quantize.quantizer import quantize

    ctx.report("quantize", 0.0, "tracking beats")
    b = s3_beats(ctx.priors, tr, grid)
    ctx.report("quantize", 0.3, "quantizing")
    priors = _grid_for_level(ctx.priors, b)
    doc, stats = quantize(tr, b.beats, b.downbeats, priors)
    ctx.perf_s = {n.id: (n.onset_s, n.offset_s) for n in tr.notes}
    v = doc.provenance.versions
    v["beats"] = b.method
    if priors.min_grid != ctx.priors.min_grid:
        v["min_grid"] = priors.min_grid
    if b.bpm is not None:
        v["tempo_level_bpm"] = f"{b.bpm:.1f}"
        v["tempo_level_source"] = "prior" if b.from_prior else "auto"
        if b.from_prior and ctx.priors.bpm:
            # the prior is a target, not a constraint: the DP settles on the nearest tempo
            # the music supports (bpm 100 on a ♩≈71 ballad tracks at 142.9)
            v["tempo_level_prior"] = f"{ctx.priors.bpm:g}"
    if b.confidence is not None and not b.from_prior:
        v["tempo_level_confidence"] = f"{b.confidence:.2f}"
    ctx.report("quantize", 1.0, f"meter {stats.meter}, pickup {stats.pickup_beats} beats")
    return doc, stats


def s5_s6_arrange(ctx: Ctx, doc, title: str):
    """S5 hands + voices, S6 key/cleanup → ScoreDoc revision 0."""
    from m2s.notation.key_spelling import estimate_key, parse_key
    from m2s.pipeline.rederive import arrange_hands

    ctx.report("arrange", 0.0, "separating hands")
    doc.notes = [n for n in doc.notes if n.velocity >= MIN_VELOCITY]
    doc.keys = [parse_key(ctx.priors.key) if ctx.priors.key else estimate_key(doc.notes)]
    arrange_hands(doc, ctx.priors.style, use_svsep=ctx.cfg.hand_model == "svsep")
    if ctx.priors.simplify:
        from m2s.notation.simplify import simplify
        from m2s.quantize.quantizer import GRID_LEVEL

        ctx.report("arrange", 0.8, "simplifying")
        perf = ctx.perf_s or {}
        times = {n.id: perf[n.src_ids[0]] for n in doc.notes if n.src_ids and n.src_ids[0] in perf}
        grid = doc.provenance.versions.get("min_grid", ctx.priors.min_grid)
        simplify(
            doc,
            ctx.priors.simplify,
            {k: v[0] for k, v in times.items()},
            GRID_LEVEL[grid],
            {k: v[1] for k, v in times.items()},
        )
    doc.title = title
    doc.provenance.audio_sha = ctx.audio_sha
    doc.provenance.stage_keys = dict(ctx.keys)
    ctx.report("arrange", 1.0, "done")
    return doc


MIN_VELOCITY = 10  # quieter "notes" are almost always transcription ghosts
