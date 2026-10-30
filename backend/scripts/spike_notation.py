"""M0 spike: can music21 export note ids, pedal marks, triplets, ties on a grand staff?
And how fast is MuseScore CLI PDF export?

usage: python scripts/spike_notation.py OUT_DIR
"""

import re
import subprocess
import sys
import time
from fractions import Fraction
from pathlib import Path

from music21 import chord, clef, expressions, key, layout, meter, note, stream, tempo

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)

sc = stream.Score()
rh = stream.PartStaff(id="RH")
lh = stream.PartStaff(id="LH")
rh.insert(0, clef.TrebleClef())
lh.insert(0, clef.BassClef())
for p in (rh, lh):
    p.insert(0, key.Key("A"))
    p.insert(0, meter.TimeSignature("4/4"))
rh.insert(0, tempo.MetronomeMark(number=96))

# RH: quarter, triplet eighths, half note that crosses the barline (tie), dotted
rh_notes = [
    ("C#5", 0, 1),
    ("E5", 1, Fraction(1, 3)),
    ("D5", Fraction(4, 3), Fraction(1, 3)),
    ("C#5", Fraction(5, 3), Fraction(1, 3)),
    ("B4", 2, 3),  # crosses into bar 2 -> tie
    ("A4", 5, Fraction(3, 2)),
    ("G#4", Fraction(13, 2), Fraction(1, 2)),
    ("A4", 7, 1),
]
for i, (p, off, ql) in enumerate(rh_notes):
    n = note.Note(p, quarterLength=ql)
    n.id = f"n{i:03d}"
    rh.insert(off, n)

lh_items = [(["A2", "E3"], 0, 2), (["D3", "F#3"], 2, 2), (["E2", "B2"], 4, 2), (["A2", "E3"], 6, 2)]
lh_objs = []
for i, (ps, off, ql) in enumerate(lh_items):
    c = chord.Chord(ps, quarterLength=ql)
    c.id = f"c{i:03d}"
    lh.insert(off, c)
    lh_objs.append(c)

pedal_ok = hasattr(expressions, "PedalMark")
if pedal_ok:
    pm = expressions.PedalMark(lh_objs[0], lh_objs[1])
    lh.insert(0, pm)

sc.insert(0, rh)
sc.insert(0, lh)
sc.insert(0, layout.StaffGroup([rh, lh], symbol="brace"))

t0 = time.perf_counter()
fixed = sc.makeNotation()
xml_path = Path(fixed.write("musicxml", fp=out / "spike.musicxml"))
t_m21 = time.perf_counter() - t0
xml = xml_path.read_text()

print("music21 write s:", round(t_m21, 3))
print("PedalMark class:", pedal_ok, "| <pedal> in xml:", "<pedal" in xml)
print("note id attrs:", re.findall(r'<note[^>]*id="([^"]+)"', xml)[:12])
print("tuplet/time-modification:", "<time-modification>" in xml, "| ties:", xml.count("<tie type="))
print("staves:", re.findall(r"<staves>\d</staves>", xml), "| part-group brace:", "brace" in xml)

mscore = "/Applications/MuseScore 4.app/Contents/MacOS/mscore"
t0 = time.perf_counter()
r = subprocess.run(
    [mscore, "-o", str(out / "spike.pdf"), str(xml_path)], capture_output=True, text=True, timeout=120
)
print(
    "mscore rc:",
    r.returncode,
    "s:",
    round(time.perf_counter() - t0, 2),
    "pdf exists:",
    (out / "spike.pdf").exists(),
)
if r.returncode:
    print(r.stderr[-1500:])
