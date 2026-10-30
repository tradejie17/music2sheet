"""Transcription evaluation on MAESTRO v3 test pieces (audio fetched per file by HTTP range).

Note-level P/R/F1 with mir_eval: onset-only (50 ms) and onset+offset. MAESTRO MIDI
offsets are key releases (not pedal-extended), matching Transkun's default checkpoint.
"""

from __future__ import annotations

import statistics
import subprocess
from dataclasses import dataclass
from pathlib import Path

import mir_eval
import numpy as np
import pretty_midi

from m2s.engines.runner import run_engine
from m2s.eval.datasets import fetch_maestro, maestro_index
from m2s.models.artifacts import Transcription


@dataclass
class TrResult:
    piece: str
    onset_f1: float
    onset_p: float
    onset_r: float
    onoff_f1: float
    seconds: float


def _crop(src: Path, dst: Path, seconds: float) -> None:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src), "-t", str(seconds), str(dst)], check=True
    )


def _arrays(intervals, pitches):
    iv = np.array(intervals, dtype=float).reshape(-1, 2)
    return iv, np.array([mir_eval.util.midi_to_hz(p) for p in pitches])


def evaluate(root: Path, work: Path, n: int, crop_s: float, device: str, log=print) -> list[TrResult]:
    idx = [p for p in maestro_index(root) if p.split == "test"]
    step = max(1, len(idx) // n)
    pieces = idx[::step][:n]
    work.mkdir(parents=True, exist_ok=True)
    out = []
    for p, (audio, midi) in zip(pieces, fetch_maestro(root, pieces)):
        clip = work / f"{p.slug}.wav"
        if not clip.exists():
            _crop(audio, clip, crop_s)
        res_dir = work / f"{p.slug}.transkun"
        res_dir.mkdir(exist_ok=True)
        art = res_dir / "artifact.json"
        if not art.exists():
            run_engine("transkun", clip, res_dir, device)
        tr = Transcription.model_validate_json(art.read_text())

        gt = [n for i in pretty_midi.PrettyMIDI(str(midi)).instruments for n in i.notes if n.start < crop_s]
        ref = _arrays([[n.start, min(n.end, crop_s)] for n in gt], [n.pitch for n in gt])
        est = _arrays([[n.onset_s, n.offset_s] for n in tr.notes], [n.pitch for n in tr.notes])
        prf = mir_eval.transcription.precision_recall_f1_overlap
        on = prf(*ref, *est, onset_tolerance=0.05, offset_ratio=None)
        onoff = prf(*ref, *est, onset_tolerance=0.05)
        r = TrResult(f"{p.composer} — {p.title}", on[2], on[0], on[1], onoff[2], crop_s)
        log(
            f"{r.piece[:60]:60s} onset F1 {r.onset_f1:.3f} (P {r.onset_p:.3f} R {r.onset_r:.3f}) "
            f"on+off {r.onoff_f1:.3f}"
        )
        out.append(r)
    log(
        f"\nmean over {len(out)} pieces: onset F1 {statistics.fmean(r.onset_f1 for r in out):.4f}, "
        f"onset+offset F1 {statistics.fmean(r.onoff_f1 for r in out):.4f}"
    )
    return out
