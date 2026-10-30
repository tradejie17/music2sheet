"""Render products of a ScoreDoc, cached by doc hash (preview, PDF and exports always
come from the same ScoreDoc, so they can never disagree)."""

from __future__ import annotations

import json
import os
import threading
from collections import defaultdict
from pathlib import Path

import pretty_midi

from m2s.jobs.store import JobStore
from m2s.models.scoredoc import ScoreDoc
from m2s.render.midi_export import PEDAL_CC
from m2s.render.musicxml import render_musicxml
from m2s.render.pdf_mscore import musicxml_to_pdf


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


# The editor requests score.musicxml and idmap.json in parallel; one lock per render
# dir makes the second request wait for (and reuse) the first render.
_locks: defaultdict[Path, threading.Lock] = defaultdict(threading.Lock)


def ensure_musicxml(store: JobStore, job_id: str, doc: ScoreDoc) -> Path:
    d = store.render_dir(job_id, doc)
    xml_path = d / "score.musicxml"
    with _locks[d]:
        if not xml_path.exists():
            xml, idmap = render_musicxml(doc)
            _atomic_write(d / "idmap.json", json.dumps(idmap))
            _atomic_write(xml_path, xml)
    return d


def ensure_pdf(store: JobStore, job_id: str, doc: ScoreDoc) -> Path:
    d = ensure_musicxml(store, job_id, doc)
    pdf = d / "score.pdf"
    if not pdf.exists():
        musicxml_to_pdf(d / "score.musicxml", pdf)
    return pdf


def doc_to_midi(doc: ScoreDoc, out: Path) -> Path:
    """Quantized MIDI: score ticks at the document's (single) tempo."""
    bpm = doc.tempos[0].bpm if doc.tempos else 120.0
    sec_per_tick = 60.0 / bpm / doc.ppq
    pm = pretty_midi.PrettyMIDI(resolution=doc.ppq, initial_tempo=bpm)
    rh = pretty_midi.Instrument(program=0, name="Piano RH")
    lh = pretty_midi.Instrument(program=0, name="Piano LH")
    for n in doc.notes:
        inst = rh if n.staff == 1 else lh
        inst.notes.append(
            pretty_midi.Note(
                velocity=n.velocity,
                pitch=n.pitch,
                start=n.start * sec_per_tick,
                end=(n.start + n.dur) * sec_per_tick,
            )
        )
    for p in doc.pedals:
        cc = PEDAL_CC[p.kind]
        lh.control_changes.append(pretty_midi.ControlChange(cc, 127, p.start * sec_per_tick))
        lh.control_changes.append(pretty_midi.ControlChange(cc, 0, p.end * sec_per_tick))
    lh.control_changes.sort(key=lambda c: c.time)
    pm.instruments += [rh, lh]
    pm.write(str(out))
    return out
