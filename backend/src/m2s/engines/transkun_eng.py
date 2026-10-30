"""Transkun V2 piano transcription (notes + sustain/soft pedal).

The shipped checkpoint does not extend notes by the sustain pedal, so offsets are
physical key releases — exactly what notation wants ("finger durations").
Transkun gives no per-note confidence; we leave it None.
"""

from __future__ import annotations

from importlib import resources
from importlib.metadata import version
from pathlib import Path

import numpy as np
import soundfile as sf

from m2s.engines.base import log, report
from m2s.models.artifacts import NoteEvent, PedalEvent, Transcription

PEDAL_CC = {64: "sustain", 67: "soft", 66: "sostenuto"}


class TranskunEngine:
    name = "transkun"
    version = version("transkun")

    def run(self, audio: Path, out_dir: Path, device: str) -> None:
        import moduleconf
        import soxr
        import torch

        report(0.02, "loading transkun")
        pre = resources.files("transkun") / "pretrained"
        conf_mgr = moduleconf.parseFromFile(str(pre / "2.0.conf"))
        model_cls = conf_mgr["Model"].module.TransKun
        ckpt = torch.load(str(pre / "2.0.pt"), map_location="cpu")
        model = model_cls(conf=conf_mgr["Model"].config)
        model.load_state_dict(ckpt.get("best_state_dict", ckpt.get("state_dict")), strict=False)
        model = model.to(device).eval()
        torch.set_grad_enabled(False)

        y, fs = sf.read(audio, dtype="float32", always_2d=True)
        if fs != model.fs:
            y = soxr.resample(y, fs, model.fs)
        duration = len(y) / model.fs
        report(0.1, f"transcribing {duration:.0f}s on {device}")
        log(f"transkun: {duration:.1f}s audio on {device}")
        raw = model.transcribe(torch.from_numpy(np.ascontiguousarray(y)).to(device), discardSecondHalf=False)
        report(0.95, "writing")

        notes, pedals = [], []
        for n in sorted(raw, key=lambda n: (n.start, n.pitch)):
            if n.pitch > 0:
                notes.append(
                    NoteEvent(
                        id=f"e{len(notes):05d}",
                        pitch=int(n.pitch),
                        onset_s=float(n.start),
                        offset_s=float(n.end),
                        velocity=int(n.velocity),
                        engine=self.name,
                    )
                )
            elif -n.pitch in PEDAL_CC:
                pedals.append(
                    PedalEvent(
                        id=f"p{len(pedals):04d}",
                        kind=PEDAL_CC[-n.pitch],
                        start_s=float(n.start),
                        end_s=float(n.end),
                    )
                )
        tr = Transcription(
            engine=self.name, engine_version=self.version, duration_s=duration, notes=notes, pedals=pedals
        )
        (out_dir / "artifact.json").write_text(tr.model_dump_json())
