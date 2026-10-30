"""API tests without ML: a finished job is simulated by writing a ScoreDoc directly."""

import pytest
from fastapi.testclient import TestClient

from m2s.config import settings
from m2s.jobs.db import JobDB
from m2s.jobs.store import JobStore
from m2s.models.priors import Priors
from m2s.models.scoredoc import KeyChange, MeterChange, ScoreDoc, ScoreNote


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("M2S_DATA_DIR", str(tmp_path))
    settings.cache_clear()
    from m2s.api.app import app

    cfg = settings()
    db, store = JobDB(cfg.db_path), JobStore(cfg.jobs_dir)
    job = db.create(title="t", source="t.wav", params=Priors().model_dump())
    doc = ScoreDoc(
        title="t",
        meters=[MeterChange(tick=0, num=4, den=4)],
        keys=[KeyChange(tick=0, fifths=0)],
        tempos=[{"tick": 0, "bpm": 100}],
        notes=[
            ScoreNote(id="a", pitch=72, start=0, dur=960),
            ScoreNote(id="b", pitch=76, start=960, dur=960),
            ScoreNote(id="c", pitch=48, start=0, dur=1920, staff=2),
        ],
    )
    store.save_doc(job["id"], doc)
    db.update(job["id"], status="done", doc_rev=0)
    yield TestClient(app), job["id"]
    settings.cache_clear()


def test_get_and_put_doc_with_optimistic_lock(env):
    client, jid = env
    doc = client.get(f"/api/jobs/{jid}/doc").json()
    assert doc["revision"] == 0 and len(doc["notes"]) == 3
    doc["notes"][0]["pitch"] = 74
    r = client.put(f"/api/jobs/{jid}/doc", json={"doc": doc})
    assert r.status_code == 200 and r.json()["revision"] == 1
    r = client.put(f"/api/jobs/{jid}/doc", json={"doc": doc})  # still based on rev 0
    assert r.status_code == 409 and r.json()["current"] == 1
    assert client.get(f"/api/jobs/{jid}").json()["doc_rev"] == 1


def test_put_rejects_invalid_doc(env):
    client, jid = env
    doc = client.get(f"/api/jobs/{jid}/doc").json()
    doc["notes"].append(dict(doc["notes"][0]))  # duplicate id
    assert client.put(f"/api/jobs/{jid}/doc", json={"doc": doc}).status_code == 422


def test_rederive_keeps_locked_staff(env):
    client, jid = env
    doc = client.get(f"/api/jobs/{jid}/doc").json()
    doc["notes"][2].update(staff=1, locked=["staff"])  # user forced the bass note into RH
    client.put(f"/api/jobs/{jid}/doc", json={"doc": doc})
    d2 = client.post(f"/api/jobs/{jid}/doc/rederive", json={"what": "hands"}).json()
    assert d2["revision"] == 2
    assert next(n for n in d2["notes"] if n["id"] == "c")["staff"] == 1


def test_renders_and_midi(env):
    client, jid = env
    xml = client.get(f"/api/jobs/{jid}/render/musicxml")
    assert xml.status_code == 200 and "<score-partwise" in xml.text
    idmap = client.get(f"/api/jobs/{jid}/render/idmap").json()
    assert set(idmap.values()) == {"a", "b", "c"}
    midi = client.get(f"/api/jobs/{jid}/midi?kind=quantized")
    assert midi.status_code == 200 and midi.content[:4] == b"MThd"
    # old revisions stay renderable
    assert client.get(f"/api/jobs/{jid}/render/musicxml?rev=0").status_code == 200


def test_create_job_validation(env):
    client, _ = env
    assert client.post("/api/jobs", data={"priors": "{}"}).status_code == 422  # neither file nor url
    r = client.post("/api/jobs", files={"file": ("x.txt", b"hello")}, data={"priors": "{}"})
    assert r.status_code == 422
    r = client.post("/api/jobs", files={"file": ("x.wav", b"RIFF")}, data={"priors": '{"time_sig": "4-4"}'})
    assert r.status_code == 422
    r = client.post("/api/jobs", files={"file": ("x.wav", b"RIFF")}, data={"priors": '{"time_sig": "3/4"}'})
    assert r.status_code == 200 and r.json()["status"] == "queued"


def test_cancel_and_rerun(env):
    client, jid = env
    r = client.post(f"/api/jobs/{jid}/rerun", json=Priors(time_sig="3/4").model_dump())
    assert r.json()["status"] == "queued" and r.json()["params"]["time_sig"] == "3/4"
    assert client.post(f"/api/jobs/{jid}/rerun", json={}).status_code == 409  # already queued
    assert client.post(f"/api/jobs/{jid}/cancel").json()["status"] == "cancelled"


def test_piano_sample_route_serves_cache_and_rejects_bad_names(env, tmp_path):
    client, _ = env
    d = tmp_path / "assets" / "piano" / "splendid"
    d.mkdir(parents=True)
    (d / "FF A#2.ogg").write_bytes(b"OggS")
    r = client.get("/api/assets/piano/FF%20A%232.ogg")
    assert r.status_code == 200 and r.content == b"OggS" and "immutable" in r.headers["cache-control"]
    assert client.get("/api/assets/piano/..%2F..%2Fm2s.sqlite3").status_code == 404


def test_rerun_with_simplify_level_and_edit_marker(env):
    client, jid = env
    doc = client.get(f"/api/jobs/{jid}/doc").json()
    assert "edited" not in doc["provenance"]["versions"]  # as generated
    client.put(f"/api/jobs/{jid}/doc", json={"doc": doc})
    assert client.get(f"/api/jobs/{jid}/doc").json()["provenance"]["versions"]["edited"] == "1"
    params = client.get(f"/api/jobs/{jid}").json()["params"]
    assert params["simplify"] == 0  # jobs from before the option read as faithful
    assert client.post(f"/api/jobs/{jid}/rerun", json={**params, "simplify": 3}).status_code == 422
    r = client.post(f"/api/jobs/{jid}/rerun", json={**params, "simplify": 2, "bpm": 130.4})
    assert r.status_code == 200
    job = r.json()
    assert job["status"] == "queued" and job["params"]["simplify"] == 2 and job["params"]["bpm"] == 130.4
    assert Priors.model_validate(job["params"]).simplify == 2  # what the worker will run with


def test_new_jobs_default_to_the_readable_score(env):
    client, _ = env
    r = client.post("/api/jobs", files={"file": ("x.wav", b"RIFF")}, data={"priors": "{}"})
    assert r.status_code == 200 and r.json()["params"]["simplify"] == 1
    r = client.post("/api/jobs", files={"file": ("x.wav", b"RIFF")}, data={"priors": '{"simplify": 0}'})
    assert r.json()["params"]["simplify"] == 0  # an explicit choice is kept
