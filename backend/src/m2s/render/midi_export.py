"""MIDI export: raw (performance, seconds) and quantized (from ScoreDoc ticks)."""

from __future__ import annotations

from pathlib import Path

import pretty_midi

from m2s.models.artifacts import Transcription

PEDAL_CC = {"sustain": 64, "sostenuto": 66, "soft": 67}


def transcription_to_midi(tr: Transcription, out: Path) -> None:
    pm = pretty_midi.PrettyMIDI(resolution=960)
    inst = pretty_midi.Instrument(program=0, name="Piano")
    for n in tr.notes:
        inst.notes.append(
            pretty_midi.Note(velocity=n.velocity, pitch=n.pitch, start=n.onset_s, end=n.offset_s)
        )
    for p in tr.pedals:
        cc = PEDAL_CC[p.kind]
        inst.control_changes.append(pretty_midi.ControlChange(cc, 127, p.start_s))
        inst.control_changes.append(pretty_midi.ControlChange(cc, 0, p.end_s))
    inst.control_changes.sort(key=lambda c: c.time)
    pm.instruments.append(inst)
    pm.write(str(out))
