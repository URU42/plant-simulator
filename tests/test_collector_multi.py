"""여러 공장을 접두어로 서빙하는 수집 서버 (다중 공장 설계 §4.7)."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from collector.app import create_app
from collector.plants import PlantEntry, PlantsConfig
from common.message import IngestBatch, Sample

T0 = datetime(2026, 11, 9, 1, 0, tzinfo=timezone.utc)


@pytest.fixture
def client(plant_path, runtime_path, rfcc_plant_path, rfcc_runtime_path, tmp_path):
    cfg = PlantsConfig(plants=[
        PlantEntry(key="rhds", plant=str(plant_path), runtime=str(runtime_path), db=str(tmp_path / "rhds.db")),
        PlantEntry(key="rfcc", plant=str(rfcc_plant_path), runtime=str(rfcc_runtime_path), db=str(tmp_path / "rfcc.db")),
    ])
    with TestClient(create_app(cfg)) as c:
        yield c


def _send(client, prefix, equip_id, tag_id, start, count, step_sec, value):
    samples = [Sample(equip_id=equip_id, tag_id=tag_id, ts=start + timedelta(seconds=i * step_sec), value=value, status="Good") for i in range(count)]
    batch = IngestBatch(source_id="T", batch_id=f"b-{prefix}-{tag_id}-{start.timestamp()}", sent_at=start, samples=samples)
    return client.post(f"{prefix}/ingest", content=batch.model_dump_json(), headers={"content-type": "application/json"})


def test_two_plants_are_isolated(client):
    assert _send(client, "/rfcc", "P-1101A", "VI-P-1101A", T0, 3, 1, 1.45).json()["stored"] == 3
    r = _send(client, "/rhds", "P-1101A", "VI-P-1101A", T0, 1, 1, 1.45).json()      # rhds 에는 없는 키
    assert r["stored"] == 0 and "unknown tag P-1101A/VI-P-1101A" in r["reasons"][0]["reason"]
    assert client.get("/rfcc/equipment/P-1101A/tags/VI-P-1101A/current").json()["status"] == "Good"
    assert client.get("/rhds/equipment/P-1101A/tags/VI-P-1101A/current").status_code == 404
    assert client.get("/rhds/health").json()["sim_now"] is None and client.get("/rfcc/health").json()["sim_now"] is not None


def test_root_health_and_index(client):
    h = client.get("/health").json()
    assert set(h["plants"]) == {"rhds", "rfcc"} and h["plants"]["rfcc"]["plant_id"] == "PL-RFCC"
    index = client.get("/")
    assert index.status_code == 200 and '/rfcc/' in index.text and 'PL-RHDS' in index.text
    assert client.get("/rfcc", follow_redirects=False).status_code == 307
    assert client.get("/nope/health").status_code == 404
    page = client.get("/rfcc/").text
    assert 'fetch("equipment")' in page and 'EventSource("stream")' in page


def test_equipment_listing_carries_master_fields(client):
    tags = {(e["equip_id"], t["tag_id"]): t for e in client.get("/rhds/equipment").json() for t in e["tags"]}
    t = tags[("P-3101", "VI-A-P3101")]
    assert t["limits"] == {"warn_hi": 4.5, "alarm_hi": 7.1, "trip_hi": 11} and "normal_ref" in t and "limit_basis" in t
    assert t["control"] == {"mean": 1.65, "sigma": 0.04614035877985053}
