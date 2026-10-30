"""beat_this (CPJKU, ISMIR 2024) beat + downbeat tracking, no madmom DBN.

Besides the peak-picked beats (`artifact.json`), the frame-wise beat/downbeat logits
are saved to `activations.npz` (50 fps) so stage S3 can re-track beats with a
tempo-continuity DP (quantize/beatdp.py) without re-running the network.
"""

from __future__ import annotations

from importlib.metadata import version
from pathlib import Path

import numpy as np
import soundfile as sf

from m2s.engines.base import report
from m2s.models.artifacts import BeatGrid

ACTIVATIONS_FILE = "activations.npz"
FPS = 50  # beat_this frame rate


class BeatThisEngine:
    name = "beat_this"
    version = version("beat-this")

    def run(self, audio: Path, out_dir: Path, device: str) -> None:
        from beat_this.inference import Audio2Frames
        from beat_this.model.postprocessor import Postprocessor

        report(0.05, "loading beat_this")
        a2f = Audio2Frames(checkpoint_path="final0", device=device)
        y, sr = sf.read(audio, dtype="float32")
        if y.ndim == 2:
            y = y.mean(axis=1)
        report(0.2, "tracking beats")
        beat_logits, down_logits = a2f(y, sr)
        beats, downbeats = Postprocessor(type="minimal", fps=FPS)(beat_logits, down_logits)
        np.savez_compressed(
            out_dir / ACTIVATIONS_FILE,
            beat=beat_logits.detach().cpu().numpy().astype(np.float32),
            downbeat=down_logits.detach().cpu().numpy().astype(np.float32),
            fps=np.float32(FPS),
        )
        grid = BeatGrid(
            beats_s=[float(b) for b in beats],
            downbeats_s=[float(d) for d in downbeats],
            source=f"{self.name}-{self.version}",
            activations=ACTIVATIONS_FILE,
        )
        (out_dir / "artifact.json").write_text(grid.model_dump_json())
