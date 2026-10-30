"""M0 spike: note F1 of Transkun CPU/MPS outputs vs MAESTRO ground truth and vs each other.

usage: python scripts/spike_compare.py SPIKE_DIR MAESTRO_DIR
"""

import json
import sys
from pathlib import Path

import mir_eval
import numpy as np
import pretty_midi

spike, maestro = Path(sys.argv[1]), Path(sys.argv[2])


def arr(notes):
    notes = [n for n in notes if n[0] > 0]
    iv = np.array([[n[1], n[2]] for n in notes]).reshape(-1, 2)
    p = np.array([mir_eval.util.midi_to_hz(n[0]) for n in notes])
    return iv, p


def f1(ref, est, offset=False):
    kw = {} if offset else {"offset_ratio": None}
    return mir_eval.transcription.precision_recall_f1_overlap(*ref, *est, onset_tolerance=0.05, **kw)[2]


for cpu_file in sorted(spike.glob("tk_cpu_*wav--*.json")):
    suffix = cpu_file.stem.removeprefix("tk_cpu_")
    mps_file = spike / f"tk_mps_{suffix}.json"
    midi = next(maestro.glob(f"*{suffix}.midi"))
    pm = pretty_midi.PrettyMIDI(str(midi))
    gt = arr([[n.pitch, n.start, n.end, n.velocity] for i in pm.instruments for n in i.notes])
    cpu = json.load(open(cpu_file))
    row = {"piece": suffix, "cpu_rtf": round(cpu["rtf"], 3)}
    c = arr(cpu["notes"])
    row["cpu_F1_onset"] = round(f1(gt, c), 4)
    # MAESTRO GT offsets are physical key releases too (no pedal extension) -> comparable
    row["cpu_F1_onoff"] = round(f1(gt, c, offset=True), 4)
    if mps_file.exists():
        mps = json.load(open(mps_file))
        m = arr(mps["notes"])
        row |= {
            "mps_rtf": round(mps["rtf"], 3),
            "mps_gb": round(mps["mps_driver_gb"], 2),
            "mps_F1_onset": round(f1(gt, m), 4),
            "cpu_vs_mps_F1": round(f1(c, m, offset=True), 4),
        }
    print(json.dumps(row))
