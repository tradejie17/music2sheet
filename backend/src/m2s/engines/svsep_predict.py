"""Staff prediction with piano_svsep. Runs INSIDE the svsep venv (no m2s imports).

usage: <svsep python> svsep_predict.py SVSEP_SRC_DIR IN.musicxml OUT.json
Writes [[onset_quarter, midi_pitch, staff (1|2)], ...] for every note fragment (partitura
renumbers note ids, so the caller maps back by onset + pitch). Input staves are not used
as features.
"""

import json
import os
import sys
import warnings

warnings.filterwarnings("ignore")


def main() -> None:
    src, score_path, out_path = sys.argv[1:4]
    sys.path.insert(0, os.path.join(src, "launch_scripts"))
    import torch
    import torch_geometric as pyg
    from piano_svsep.models.pl_models import PLPianoSVSep
    from predict import prepare_score

    model = PLPianoSVSep.load_from_checkpoint(
        os.path.join(src, "pretrained_models", "model.ckpt"),
        map_location="cpu",
        strict=False,
        weights_only=False,
    )
    model.module.eval()
    graph, score, _ = prepare_score(score_path, include_original=False)
    na = score[0].note_array()
    with torch.no_grad():
        _, pred_staff, _ = model.predict_step(pyg.data.Batch.from_data_list([graph]), return_graph=True)
    staff = pred_staff.cpu().numpy().astype(int) + 1
    out = [[float(q), int(p), int(s)] for q, p, s in zip(na["onset_quarter"], na["pitch"], staff)]
    with open(out_path, "w") as f:
        json.dump(out, f)


if __name__ == "__main__":
    main()
