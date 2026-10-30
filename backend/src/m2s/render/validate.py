"""Strict MusicXML sanity check, mirroring how MuseScore measures notes.

MuseScore derives a note's length from <type>, <dot> and <time-modification>, not
from <duration>; a voice whose written values don't add up to the bar makes the
import fail ("Voice too long" / "Incomplete measure"). We check the same thing so
problems surface in tests instead of at PDF time.
"""

from __future__ import annotations

from collections import defaultdict
from fractions import Fraction

from lxml import etree

TYPE_QL = {
    "whole": Fraction(4), "half": Fraction(2), "quarter": Fraction(1), "eighth": Fraction(1, 2),
    "16th": Fraction(1, 4), "32nd": Fraction(1, 8), "64th": Fraction(1, 16),
}  # fmt: skip


def written_ql(n) -> Fraction | None:
    t = n.findtext("type")
    if t is None:  # whole-measure rest
        return None
    q = TYPE_QL[t]
    q = q * (2 - Fraction(1, 2 ** len(n.findall("dot"))))
    tm = n.find("time-modification")
    if tm is not None:
        q = q * int(tm.findtext("normal-notes")) / int(tm.findtext("actual-notes"))
    return q


def check_measures(xml: str) -> list[str]:
    """Return human-readable problems (empty list = OK)."""
    root = etree.fromstring(xml.encode())
    problems = []
    for part in root.iter("part"):
        beats = Fraction(4)
        for m in part.iter("measure"):
            ts = m.find("attributes/time")
            if ts is not None:
                beats = Fraction(int(ts.findtext("beats")) * 4, int(ts.findtext("beat-type")))
            implicit = m.get("implicit") == "yes"
            totals: dict[tuple[str, str], Fraction] = defaultdict(Fraction)
            for n in m.iter("note"):
                if n.find("chord") is not None or n.find("grace") is not None:
                    continue
                q = written_ql(n)
                key = (n.findtext("staff") or "1", n.findtext("voice") or "1")
                totals[key] += beats if q is None else q
            open_tuplet: dict[tuple[str, str], bool] = defaultdict(bool)
            for n in m.iter("note"):
                if n.find("chord") is not None:  # chord members share the head note's bracket
                    continue
                key = (n.findtext("staff") or "1", n.findtext("voice") or "1")
                types = [t.get("type") for t in n.iter("tuplet")]
                if "start" in types:
                    open_tuplet[key] = True
                if n.find("time-modification") is not None and not open_tuplet[key]:
                    where = f"measure {m.get('number')} staff {key[0]} voice {key[1]}"
                    problems.append(f"{where}: tuplet note outside bracket")
                if "stop" in types:
                    open_tuplet[key] = False
            for (st, v), tot in totals.items():
                if tot > beats or (tot != beats and not implicit):
                    problems.append(f"measure {m.get('number')} staff {st} voice {v}: {tot} != {beats}")
    return problems
