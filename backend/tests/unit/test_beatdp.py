"""Tempo-continuity beat DP (quantize/beatdp.py) on synthetic activations."""

from pathlib import Path

import numpy as np
import pytest

from m2s.eval.beats import jump_fraction
from m2s.models.artifacts import BeatGrid, NoteEvent, Transcription
from m2s.models.priors import Priors
from m2s.pipeline.stages import Ctx, s3_beats, s3_s4_quantize
from m2s.quantize.beatdp import Activations, choose_bar_grid, dp_track, load_activations, track
from m2s.quantize.beatmap import clean_beats

FPS = 50.0


def _bumps(n: int, times: np.ndarray, height: float = 0.9) -> np.ndarray:
    x = np.full(n, 0.01)
    fr = np.arange(n)
    for t in times:
        x = np.maximum(x, height * np.exp(-0.5 * ((fr - t * FPS) / 1.2) ** 2))
    return x


def ballad(seconds: float = 120.0, bpm: float = 65.0, drift: float = 0.0):
    """Beats at `bpm` (optionally drifting); the tracker's activation is at the beat level in
    the first third, double tempo in the middle and half tempo at the end (level switching).
    Downbeat peaks every 2 beats, stronger on every 4th (4/4 whose half-bars the tracker
    also hears as downbeats); four rhythmic events per beat."""
    n = int(seconds * FPS)
    k = np.arange(int(seconds * bpm / 60 * 1.5))
    local = bpm * (1 + drift * np.sin(2 * np.pi * k / 80))
    beats = 0.5 + np.concatenate([[0.0], np.cumsum(60 / local[:-1])])
    beats = beats[beats < seconds - 0.5]
    third = seconds / 3
    mids = (beats[:-1] + beats[1:]) / 2
    act_times = np.concatenate(
        [
            beats[beats < 2 * third],
            mids[(mids >= third) & (mids < 2 * third)],  # double-time section
            beats[beats >= 2 * third][::2],  # half-time section
        ]
    )
    down = np.maximum(_bumps(n, beats[::4], 0.75), _bumps(n, beats[2::4], 0.55))
    act = Activations(beat=_bumps(n, act_times), downbeat=down, fps=FPS)
    onsets = np.sort(
        np.concatenate([beats[:-1] + (beats[1:] - beats[:-1]) * f for f in (0, 0.25, 0.5, 0.75)])
    )
    raw = np.sort(act_times)
    return act, beats, raw, onsets


def test_dp_keeps_one_level_through_tracker_level_switches():
    act, beats, raw, onsets = ballad()
    assert jump_fraction(raw) > 0.01  # the tracker's own peaks switch level
    r = track(act, onsets, onsets[0], onsets[-1])
    assert jump_fraction(r.beats) == 0.0
    assert 60 < r.bpm < 70
    # every true beat found within 40 ms
    d = np.min(np.abs(beats[1:-1, None] - r.beats[None, :]), axis=1)
    assert np.mean(d < 0.04) > 0.97


def test_dp_follows_rubato():
    act, beats, raw, onsets = ballad(drift=0.2)
    r = track(act, onsets, onsets[0], onsets[-1])
    d = np.min(np.abs(beats[1:-1, None] - r.beats[None, :]), axis=1)
    assert np.mean(d < 0.04) > 0.95
    assert jump_fraction(r.beats) < 0.02


@pytest.mark.parametrize("bpm,expect", [(130.0, 130.0), (65.0, 65.0), (32.5, 32.5)])
def test_bpm_prior_selects_the_level(bpm, expect):
    act, _, raw, onsets = ballad()
    r = track(act, onsets, onsets[0], onsets[-1], bpm_prior=bpm)
    assert r.from_prior
    assert abs(np.log2(r.bpm / expect)) < 0.1
    assert jump_fraction(r.beats) < 0.05


def test_dp_track_constant_period():
    reward = _bumps(3000, np.arange(0.4, 59, 0.5))
    fr = dp_track(reward, 25.0)
    assert np.all(np.abs(np.diff(fr) - 25) <= 1)


def test_bar_grid():
    sal = np.tile([0.9, 0.1, 0.2], 20)
    assert choose_bar_grid(sal)[:2] == (3, 0)
    sal = np.tile([0.1, 0.8, 0.1, 0.2], 20)
    assert choose_bar_grid(sal)[:2] == (4, 1)
    # downbeat peaks every 2 beats, equally strong: 2 beats per bar
    sal = np.tile([0.8, 0.1], 40)
    assert choose_bar_grid(sal)[0] == 2
    # ... but if every other one is stronger, the half-bars are inside a 4-beat bar
    sal = np.tile([0.1, 0.8, 0.1, 0.6], 20)
    assert choose_bar_grid(sal)[:2] == (4, 1)
    # a time-signature prior fixes beats per bar, the salience picks the phase
    sal = np.tile([0.1, 0.1, 0.9], 20)
    assert choose_bar_grid(sal, 3)[:2] == (3, 2)


def test_load_activations_missing(tmp_path: Path):
    assert load_activations(None) is None
    assert load_activations(tmp_path / "nope.npz") is None


def _transcription(onsets: np.ndarray) -> Transcription:
    notes = [
        NoteEvent(
            id=f"e{i:05d}",
            pitch=60 + (i % 12),
            onset_s=float(t),
            offset_s=float(t) + 0.2,
            velocity=60,
            engine="t",
        )
        for i, t in enumerate(onsets)
    ]
    return Transcription(engine="t", engine_version="0", duration_s=float(onsets[-1]) + 1, notes=notes)


def _save(tmp_path: Path, act: Activations) -> str:
    logit = lambda p: np.log(p / (1 - p))  # noqa: E731
    path = tmp_path / "activations.npz"
    np.savez(path, beat=logit(act.beat), downbeat=logit(act.downbeat), fps=np.float32(act.fps))
    return str(path)


def test_s3_uses_dp_when_activations_exist(tmp_path: Path):
    act, _, raw, onsets = ballad(seconds=60)
    tr = _transcription(onsets)
    grid = BeatGrid(beats_s=raw.tolist(), downbeats_s=[], source="t", activations=_save(tmp_path, act))
    b = s3_beats(Priors(), tr, grid)
    assert b.method == "dp" and jump_fraction(b.beats) == 0.0
    doc, stats = s3_s4_quantize(Ctx(audio=Path("/dev/null"), audio_sha="0" * 64, priors=Priors()), tr, grid)
    assert doc.provenance.versions["beats"] == "dp"
    assert 60 < doc.tempos[0].bpm < 70
    assert stats.meter == "4/4"
    # tempo ×2 from the editor = rerun with priors.bpm doubled
    b2 = s3_beats(Priors(bpm=130), tr, grid)
    assert abs(len(b2.beats) / len(b.beats) - 2) < 0.1


def test_s3_falls_back_without_activations():
    act, _, raw, onsets = ballad(seconds=60)
    tr = _transcription(onsets)
    grid = BeatGrid(beats_s=raw.tolist(), downbeats_s=[], source="t")
    b = s3_beats(Priors(), tr, grid)
    assert b.method == "legacy"
    want = clean_beats(grid, None, float(onsets.min()), max(n.offset_s for n in tr.notes))
    assert np.array_equal(b.beats, want)
    # a dangling path (cache entry removed) falls back too
    b = s3_beats(Priors(), tr, grid.model_copy(update={"activations": "/nonexistent/activations.npz"}))
    assert b.method == "legacy"


def test_old_beat_artifacts_still_load():
    g = BeatGrid.model_validate_json(
        '{"beats_s": [0.5, 1.0], "downbeats_s": [0.5], "source": "beat_this-0.1"}'
    )
    assert g.activations is None


def test_tempo_mark_counts_quarters_in_compound_meter(tmp_path: Path):
    """TempoMark.bpm is quarter notes per minute: a dotted-quarter beat at 65 is ♩=97.5
    (engraved ♩.=65); it used to come out 2.25× too low (♩.≈29)."""
    act, _, raw, onsets = ballad(seconds=60)
    tr = _transcription(onsets)
    grid = BeatGrid(beats_s=raw.tolist(), downbeats_s=[], source="t", activations=_save(tmp_path, act))
    ctx = Ctx(audio=Path("/dev/null"), audio_sha="0" * 64, priors=Priors(time_sig="6/8"))
    doc, stats = s3_s4_quantize(ctx, tr, grid)
    beat_bpm = float(doc.provenance.versions["tempo_level_bpm"])
    assert stats.model == "compound"
    assert doc.tempos[0].bpm == pytest.approx(beat_bpm * 1.5, rel=0.03)


def test_halved_ballad_stays_simple(tmp_path: Path, monkeypatch):
    """Tempo ÷2 on a ballad with 32nd-note runs: at the half level the runs are 64ths, which
    a ÷12 compound grid fits better than ÷8 simple. The half-beat check keeps it simple
    (Call of Silence ÷2 came out as 12/8 with ♩.=15)."""
    import m2s.quantize.quantizer as Q

    act, beats, raw, _ = ballad(seconds=60)
    on = np.sort(np.concatenate([beats[:-1] + (beats[1:] - beats[:-1]) * f / 8 for f in range(8)]))
    tr = _transcription(on)
    grid = BeatGrid(beats_s=raw.tolist(), downbeats_s=[], source="t", activations=_save(tmp_path, act))
    ctx = Ctx(audio=Path("/dev/null"), audio_sha="0" * 64, priors=Priors(bpm=32.5))
    doc, stats = s3_s4_quantize(ctx, tr, grid)
    assert stats.model == "simple" and stats.meter in ("4/4", "2/4")
    assert doc.tempos[0].bpm == pytest.approx(32.5, rel=0.05)
    assert doc.provenance.versions.get("min_grid") == "1/32"  # the grid follows the slow level
    # without the half-beat check the ÷8-vs-÷12 comparison picks compound here
    monkeypatch.setattr(Q, "SLOW_BEAT_S", 99.0)
    assert s3_s4_quantize(ctx, tr, grid)[1].model == "compound"


def test_repair_bar_phase_removes_an_extra_beat():
    """3/4, 40 bars; the beat track has one extra beat in bar 20 (a fermata), so a single
    global phase puts every later bar line one beat early. The repair drops the weaker of
    the two beats that share a bar position and the bar grid fits the whole piece again."""
    from m2s.quantize.beatdp import repair_bar_phase

    true = np.arange(120) * 0.5
    extra = true[60] + 0.25
    beats = np.sort(np.append(true, extra))
    sal = np.where(np.isin(beats, true[::3]), 0.8, 0.1)
    act = np.where(beats == extra, 0.3, 0.9)
    bpb, phase, _ = choose_bar_grid(sal)
    assert bpb == 3
    fixed, ph = repair_bar_phase(beats, sal, act, bpb, phase, penalty=1.5)
    assert np.array_equal(fixed, true)
    assert np.array_equal(fixed[ph::3], true[::3])
    # a missing beat is put back halfway
    gap = np.delete(true, 61)
    sal = np.where(np.isin(gap, true[::3]), 0.8, 0.1)
    fixed, ph = repair_bar_phase(gap, sal, np.full(len(gap), 0.9), 3, 0, penalty=1.5)
    assert np.allclose(fixed, true) and ph == 0
    # weak, noisy downbeat evidence never pays the penalty
    rng = np.random.default_rng(0)
    sal = 0.3 + 0.05 * rng.standard_normal(len(true))
    assert np.array_equal(repair_bar_phase(true, sal, np.full(len(true), 0.9), 3, 0, 1.5)[0], true)
