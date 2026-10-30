"""M0 spike: Transkun speed/memory on CPU vs MPS.

usage: python scripts/spike_transkun.py AUDIO OUT.json --device cpu|mps [--seconds N]
"""

import argparse
import json
import resource
import time
from importlib import resources

import moduleconf
import numpy as np
import soundfile as sf
import soxr
import torch


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("out")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--seconds", type=float, default=None)
    args = ap.parse_args()

    t0 = time.perf_counter()
    pre = resources.files("transkun") / "pretrained"
    conf_mgr = moduleconf.parseFromFile(str(pre / "2.0.conf"))
    TransKun = conf_mgr["Model"].module.TransKun
    ckpt = torch.load(str(pre / "2.0.pt"), map_location="cpu")
    model = TransKun(conf=conf_mgr["Model"].config)
    model.load_state_dict(ckpt.get("best_state_dict", ckpt.get("state_dict")), strict=False)
    model = model.to(args.device).eval()
    torch.set_grad_enabled(False)
    t_load = time.perf_counter() - t0

    y, fs = sf.read(args.audio, dtype="float32", always_2d=True)
    if args.seconds:
        y = y[: int(args.seconds * fs)]
    if fs != model.fs:
        y = soxr.resample(y, fs, model.fs)
    dur = len(y) / model.fs

    t1 = time.perf_counter()
    notes = model.transcribe(
        torch.from_numpy(np.ascontiguousarray(y)).to(args.device), discardSecondHalf=False
    )
    if args.device == "mps":
        torch.mps.synchronize()
    t_inf = time.perf_counter() - t1

    rss_gb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e9  # bytes on macOS
    mps_gb = torch.mps.driver_allocated_memory() / 1e9 if args.device == "mps" else 0.0
    out = {
        "device": args.device,
        "audio_s": dur,
        "load_s": t_load,
        "infer_s": t_inf,
        "rtf": t_inf / dur,
        "peak_rss_gb": rss_gb,
        "mps_driver_gb": mps_gb,
        "n_notes": sum(n.pitch > 0 for n in notes),
        "n_pedal": sum(n.pitch < 0 for n in notes),
        "notes": [[n.pitch, n.start, n.end, n.velocity] for n in notes],
    }
    json.dump(out, open(args.out, "w"))
    print(json.dumps({k: v for k, v in out.items() if k != "notes"}, indent=1))


if __name__ == "__main__":
    main()
