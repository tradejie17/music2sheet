import pytest
from pydantic import ValidationError

from m2s.models.artifacts import TempoMap
from m2s.models.priors import Priors
from m2s.models.scoredoc import KeyChange, MeterChange, ScoreDoc, ScoreNote
from m2s.pipeline.cache import StageCache, stage_key


def test_tempo_map_roundtrip_and_extrapolation():
    tm = TempoMap(anchors=[(0, 1.0), (960, 1.5), (1920, 2.5)])
    assert tm.tick_to_sec(480) == pytest.approx(1.25)
    assert tm.sec_to_tick(2.0) == pytest.approx(1440)
    assert tm.tick_to_sec(-960) == pytest.approx(0.5)  # first-segment slope
    assert tm.sec_to_tick(3.5) == pytest.approx(2880)  # last-segment slope
    for t in (0, 100, 960, 1500, 3000):
        assert tm.sec_to_tick(tm.tick_to_sec(t)) == pytest.approx(t)


def _doc(**kw):
    base = dict(meters=[MeterChange(tick=0, num=4, den=4)], keys=[KeyChange(tick=0, fifths=0)])
    return ScoreDoc(**(base | kw))


def test_scoredoc_barlines_with_pickup_and_meter_change():
    doc = _doc(
        pickup_ticks=960, meters=[MeterChange(tick=0, num=4, den=4), MeterChange(tick=4800, num=3, den=4)]
    )
    assert doc.barline_ticks(8000)[:5] == [0, 960, 4800, 7680, 10560]


def test_scoredoc_rejects_meter_change_off_barline():
    with pytest.raises(ValidationError):
        _doc(meters=[MeterChange(tick=0, num=4, den=4), MeterChange(tick=1000, num=3, den=4)])


def test_scoredoc_rejects_duplicate_ids():
    n = ScoreNote(id="a", pitch=60, start=0, dur=960)
    with pytest.raises(ValidationError):
        _doc(notes=[n, n])


def test_priors_validation():
    Priors(key="Eb major", time_sig="6/8")
    with pytest.raises(ValidationError):
        Priors(time_sig="4-4")
    with pytest.raises(ValidationError):
        Priors(key="H major")


def test_stage_key_depends_on_params_and_upstream():
    a = stage_key("s", 1, {"x": 1}, ["u"])
    assert a == stage_key("s", 1, {"x": 1}, ["u"])
    assert a != stage_key("s", 1, {"x": 2}, ["u"])
    assert a != stage_key("s", 2, {"x": 1}, ["u"])
    assert a != stage_key("s", 1, {"x": 1}, ["v"])


def test_stage_cache_hit_and_atomic_failure(tmp_path):
    cache = StageCache(tmp_path)
    calls = []

    def build(d):
        calls.append(1)
        (d / "artifact.json").write_text("{}")

    entry, hit = cache.get_or_build("s", "k", build)
    assert not hit and (entry / "artifact.json").exists()
    _, hit = cache.get_or_build("s", "k", build)
    assert hit and len(calls) == 1

    def boom(d):
        (d / "artifact.json").write_text("partial")
        raise RuntimeError("fail")

    with pytest.raises(RuntimeError):
        cache.get_or_build("s", "k2", boom)
    assert not cache.has("s", "k2")
    assert not list((tmp_path / "s").glob(".tmp-*"))
