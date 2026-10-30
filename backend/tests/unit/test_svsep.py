import pytest

from m2s.hands.svsep import predict_staffs, svsep_paths
from m2s.models.scoredoc import KeyChange, MeterChange, ScoreDoc, ScoreNote

pytestmark = [pytest.mark.slow, pytest.mark.svsep]


@pytest.mark.skipif(svsep_paths() is None, reason="piano_svsep env not installed (scripts/setup_svsep.sh)")
def test_svsep_predicts_every_note_with_pickup():
    notes = [ScoreNote(id="pick", pitch=72, start=0, dur=960)]
    for b in range(1, 17):
        notes.append(ScoreNote(id=f"r{b}", pitch=72 + b % 5, start=b * 960, dur=960))
        notes.append(ScoreNote(id=f"l{b}", pitch=43 + b % 3, start=b * 960, dur=960, staff=2))
    doc = ScoreDoc(
        meters=[MeterChange(tick=0, num=4, den=4)],
        keys=[KeyChange(tick=0, fifths=0)],
        pickup_ticks=960,
        notes=notes,
    )
    pred = predict_staffs(doc)
    assert set(pred) == {n.id for n in notes}
    assert all(pred[f"r{b}"] == 1 and pred[f"l{b}"] == 2 for b in range(1, 17))
