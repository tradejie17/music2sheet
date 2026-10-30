"""Score-level evaluation on ASAP scores (github.com/fosfrancesco/asap-dataset, CC BY-NC-SA).

For each ground-truth piano score (MusicXML, two staves) we synthesise a performance
(tempo drift + onset jitter + a noisy tracker beat grid with dropped downbeats), run
our S3–S6 stages, and compare with the score:

  onset_acc  (pitch, onset) reproduced exactly (best whole-beat shift)
  staff_acc  hand/staff agreement on matched notes
  meter_ok   time signature equals the score's first one
  key_ok     key signature (fifths) equals the score's first one
  pickup_ok  anacrusis length equals the score's
  render_ok  our MusicXML passes the strict validator

This isolates quantization / meter / hands / key from transcription errors.
"""

from __future__ import annotations

import csv
import pickle
import statistics
import subprocess
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path

from m2s.eval.synth import make_performance
from m2s.models.priors import Priors
from m2s.pipeline.stages import Ctx, s3_s4_quantize, s5_s6_arrange
from m2s.quantize.meter import from_time_sig
from m2s.render.musicxml import render_musicxml
from m2s.render.validate import check_measures

ASAP_URL = "https://github.com/fosfrancesco/asap-dataset"


def ensure_asap(root: Path) -> Path:
    if not (root / "LICENSE.md").exists():
        root.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--depth", "1", ASAP_URL, str(root)], check=True)
    return root


@dataclass
class GTScore:
    notes: list[tuple[int, Fraction, Fraction, int]]  # pitch, start (quarters), dur, staff
    time_sig: tuple[int, int]
    fifths: int
    pickup_q: Fraction
    meter_changes: int


def load_score(path: Path, max_measures: int | None = 64) -> GTScore | None:
    from music21 import converter, key, meter, note, stream

    sc = converter.parse(str(path))
    parts = list(sc.parts)
    if len(parts) != 2:  # keep plain grand-staff piano scores
        return None
    ts = sc.recurse().getElementsByClass(meter.TimeSignature).first()
    ks = sc.recurse().getElementsByClass(key.KeySignature).first()
    if ts is None or ks is None:
        return None
    first_m = parts[0].getElementsByClass(stream.Measure).first()
    bar_q = Fraction(ts.barDuration.quarterLength)
    pickup = bar_q - Fraction(first_m.duration.quarterLength) if first_m is not None else Fraction(0)
    pickup = pickup if 0 < pickup < bar_q else Fraction(0)
    pickup_len = bar_q - pickup if pickup else Fraction(0)
    limit = None if max_measures is None else (pickup_len + bar_q * (max_measures - (1 if pickup else 0)))

    notes = []
    for staff, part in enumerate(parts, start=1):
        for el in part.stripTies().flatten().notes:
            if el.duration.isGrace or el.quarterLength == 0:
                continue
            start = Fraction(el.offset)
            if limit is not None and start >= limit:
                continue
            pitches = el.pitches if not isinstance(el, note.Note) else [el.pitch]
            for p in pitches:
                notes.append((p.midi, start, Fraction(el.quarterLength), staff))
    changes = len(
        {(t.numerator, t.denominator) for t in sc.recurse().getElementsByClass(meter.TimeSignature)}
    )
    return GTScore(notes, (ts.numerator, ts.denominator), ks.sharps, pickup_len, changes - 1)


@dataclass
class Result:
    piece: str
    notes: int
    onset_acc: float
    staff_acc: float
    meter_ok: bool
    key_ok: bool
    pickup_ok: bool
    render_ok: bool
    gt_meter: str
    our_meter: str


def evaluate_piece(
    name: str, gt: GTScore, seed: int = 0, jitter_s: float = 0.015, render: bool = True
) -> Result:
    num, den = gt.time_sig
    _, _, bpb, beat_ticks = from_time_sig(f"{num}/{den}")
    beat_q = Fraction(beat_ticks, 960)
    score = [(p, s / beat_q, d / beat_q) for p, s, d, _ in gt.notes]
    pickup_beats = int(gt.pickup_q / beat_q) if gt.pickup_q else 0
    perf = make_performance(
        score,
        bpm=90.0,
        drift=0.10,
        jitter_s=jitter_s,
        beats_per_bar=bpb,
        first_downbeat=pickup_beats,
        downbeat_dropout=0.3,
        seed=seed,
    )
    ctx = Ctx(audio=Path("/dev/null"), audio_sha="0" * 64, priors=Priors())
    doc, _ = s3_s4_quantize(ctx, perf.transcription, perf.beats)
    doc = s5_s6_arrange(ctx, doc, title=name)

    # ground truth in our tick units (960 per quarter), relative to the first note
    gt_first = min(s for _, s, _, _ in gt.notes)
    want = {(p, int((s - gt_first) * 960)): st for p, s, _, st in gt.notes}
    our_first = min(n.start for n in doc.notes)
    best_hits, best_shift = -1, 0
    step = int(doc.ppq * 4 / doc.meters[0].den) if doc.meters[0].den else 960
    for k in range(-2, 3):
        shift = k * step - our_first
        hits = sum((n.pitch, n.start + shift) in want for n in doc.notes)
        if hits > best_hits:
            best_hits, best_shift = hits, shift
    matched = [
        (n, want[(n.pitch, n.start + best_shift)])
        for n in doc.notes
        if (n.pitch, n.start + best_shift) in want
    ]
    staff_acc = sum(n.staff == st for n, st in matched) / len(matched) if matched else 0.0

    render_ok = True
    if render:
        xml, _ = render_musicxml(doc)
        render_ok = not check_measures(xml)
    m = doc.meters[0]
    return Result(
        piece=name,
        notes=len(want),
        onset_acc=round(best_hits / len(want), 4),
        staff_acc=round(staff_acc, 4),
        meter_ok=(m.num, m.den) == (num, den),
        key_ok=doc.keys[0].fifths == gt.fifths,
        pickup_ok=doc.pickup_ticks == int(gt.pickup_q * 960),
        render_ok=render_ok,
        gt_meter=f"{num}/{den}",
        our_meter=f"{m.num}/{m.den}",
    )


def crop(gt: GTScore, measures: int = 64) -> GTScore:
    bar_q = Fraction(4 * gt.time_sig[0], gt.time_sig[1])
    limit = gt.pickup_q + bar_q * (measures - (1 if gt.pickup_q else 0))
    return GTScore(
        [n for n in gt.notes if n[1] < limit], gt.time_sig, gt.fifths, gt.pickup_q, gt.meter_changes
    )


def load_all(root: Path, cache: Path, log=print) -> dict[str, GTScore]:
    """Parse every ASAP score once (music21 is slow) and cache the result."""
    if cache.exists():
        return pickle.loads(cache.read_bytes())
    ensure_asap(root)
    out = {}
    for p in sorted(root.glob("**/xml_score.musicxml")):
        name = str(p.parent.relative_to(root))
        try:
            gt = load_score(p, max_measures=None)
        except Exception as e:  # odd files: skip, keep going
            log(f"skip {name}: {e}")
            continue
        if gt is not None and gt.notes:
            out[name] = gt
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(pickle.dumps(out))
    return out


def run(root: Path, out_csv: Path, limit: int | None = None, seed: int = 0, render: bool = True, log=print):
    scores = load_all(root, out_csv.parent / "asap_gt_full.pkl", log)
    names = sorted(scores)
    if limit:
        step = max(1, len(names) // limit)
        names = names[::step][:limit]
    results: list[Result] = []
    for i, name in enumerate(names):
        try:
            r = evaluate_piece(name, crop(scores[name]), seed=seed, render=render)
        except Exception as e:  # report and keep going: one odd score shouldn't stop the run
            log(f"[{i + 1}/{len(names)}] ERROR {name}: {e}")
            continue
        results.append(r)
        log(
            f"[{i + 1}/{len(names)}] {name}: onset {r.onset_acc:.3f} staff {r.staff_acc:.3f} "
            f"meter {r.our_meter}/{r.gt_meter} key {'✓' if r.key_ok else '✗'} "
            f"render {'✓' if r.render_ok else '✗'}"
        )
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(results[0]).keys()))
        w.writeheader()
        w.writerows(asdict(r) for r in results)
    return summarize(results)


def summarize(results: list[Result]) -> dict[str, float]:
    n = len(results)
    return {
        "pieces": n,
        "onset_acc_median": statistics.median(r.onset_acc for r in results),
        "onset_acc_mean": statistics.fmean(r.onset_acc for r in results),
        "staff_acc_median": statistics.median(r.staff_acc for r in results),
        "staff_acc_mean": statistics.fmean(r.staff_acc for r in results),
        "meter_ok": sum(r.meter_ok for r in results) / n,
        "key_ok": sum(r.key_ok for r in results) / n,
        "pickup_ok": sum(r.pickup_ok for r in results) / n,
        "render_ok": sum(r.render_ok for r in results) / n,
    }
