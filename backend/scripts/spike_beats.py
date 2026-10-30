"""M0 spike: beat_this speed on CPU vs MPS, and agreement between them.

usage: python scripts/spike_beats.py AUDIO --device cpu|mps
"""

import argparse
import json
import resource
import time

import numpy as np
import soundfile as sf
import torch
from beat_this.inference import Audio2Beats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--out")
    args = ap.parse_args()

    t0 = time.perf_counter()
    a2b = Audio2Beats(checkpoint_path="final0", device=args.device, dbn=False)
    t_load = time.perf_counter() - t0
    y, sr = sf.read(args.audio, dtype="float32")
    if y.ndim == 2:
        y = y.mean(axis=1)
    t1 = time.perf_counter()
    beats, downbeats = a2b(y, sr)
    if args.device == "mps":
        torch.mps.synchronize()
    t_inf = time.perf_counter() - t1
    ibi = np.diff(beats)
    res = {
        "device": args.device,
        "audio_s": len(y) / sr,
        "load_s": t_load,
        "infer_s": t_inf,
        "peak_rss_gb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e9,
        "n_beats": len(beats),
        "n_downbeats": len(downbeats),
        "median_bpm": float(60 / np.median(ibi)) if len(ibi) else None,
        "beats_per_bar_hist": np.unique(
            [int(((beats >= a) & (beats < b)).sum()) for a, b in zip(downbeats[:-1], downbeats[1:])],
            return_counts=True,
        )[1].tolist()
        if len(downbeats) > 1
        else [],
        "beats_per_bar_vals": np.unique(
            [int(((beats >= a) & (beats < b)).sum()) for a, b in zip(downbeats[:-1], downbeats[1:])]
        ).tolist()
        if len(downbeats) > 1
        else [],
    }
    print(json.dumps(res, indent=1))
    if args.out:
        json.dump({"beats": beats.tolist(), "downbeats": downbeats.tolist(), **res}, open(args.out, "w"))


if __name__ == "__main__":
    main()
