"""수집 서버 (기획서 6.2, 7.1, 9.2 4단계): /ingest, ts_raw·tag_current 저장, 설비 마스터 적재."""
import pytest
from fastapi.testclient import TestClient

from collector.app import create_app
from common.message import IngestBatch, Sample

KST = "+09:00"


@pytest.fixture
def client(plant_path, runtime_path):
    app = create_app(plant_path=plant_path, runtime_path=runtime_path, db_path=":memory:")
    with TestClient(app) as c:
        yield c


def _batch(batch_id="b-20261108-172400-SIM-ALL-0001", samples=None):
    samples = samples or [
        Sample(equip_id="C-1101", tag_id="VI-C1101", ts=f"2026-11-08T17:23:10{KST}", value=2.34, quality="Good"),
        Sample(equip_id="C-1101", tag_id="VI-C1101", ts=f"2026-11-08T17:23:20{KST}", value=2.36, quality="Good"),
        Sample(equip_id="P-1101", tag_id="TI-P1101", ts=f"2026-11-08T17:23:10{KST}", value=None, quality="ScanOff"),
    ]
    return IngestBatch(source_id="SIM-ALL", batch_id=batch_id, sent_at=f"2026-11-08T17:24:00{KST}", samples=samples)


def _post(client, batch: IngestBatch, **overrides):
    body = batch.model_dump(mode="json") | overrides
    return client.post("/ingest", json=body)


def test_master_is_seeded_from_plant_config(client):
    storage = client.app.state.storage
    assert storage.count("equipment") == 20
    assert storage.count("equipment_tag") == 46
    assert storage.count("unit") == 6
    assert storage.count("equipment_type") == 7
    assert storage.count("tag_std") == 3
    assert storage.count("equipment_alias") >= 22
    assert ("C-1101",) in storage.query("SELECT equip_id FROM equipment_alias WHERE alias = ?", ("1호기 압축기",))


def test_ingest_stores_rows_and_updates_current(client):
    r = _post(client, _batch())
    assert r.status_code == 200
    body = r.json()
    assert body["stored"] == 3 and body["rejected"] == 0 and body["duplicate"] is False
    assert client.app.state.storage.count("ts_raw") == 3

    cur = client.get("/tags/VI-C1101/current").json()
    assert cur["value"] == 2.36 and cur["quality"] == "Good"
    assert cur["ts"] == "2026-11-08T08:23:20+00:00"   # 저장 시각은 UTC


def test_duplicate_batch_is_stored_once(client):
    _post(client, _batch())
    r = _post(client, _batch())
    assert r.json()["duplicate"] is True and r.json()["stored"] == 0
    assert client.app.state.storage.count("ts_raw") == 3


def test_same_tag_ts_in_another_batch_is_stored_once(client):
    _post(client, _batch())
    r = _post(client, _batch(batch_id="b-20261108-172500-SIM-ALL-0002"))
    assert r.json()["stored"] == 0 and r.json()["duplicate"] is False
    assert client.app.state.storage.count("ts_raw") == 3


def test_unknown_tag_and_malformed_rows_are_rejected_with_reason(client):
    body = _batch().model_dump(mode="json")
    body["samples"].append({"equip_id": "C-1101", "tag_id": "VI-NOPE", "ts": f"2026-11-08T17:23:10{KST}", "value": 1.0, "quality": "Good"})
    body["samples"].append({"equip_id": "C-1101", "tag_id": "VI-C1101", "ts": "not-a-time", "value": 1.0, "quality": "Good"})
    body["samples"].append({"equip_id": "P-1101", "tag_id": "VI-C1101", "ts": f"2026-11-08T17:23:30{KST}", "value": 1.0, "quality": "Good"})
    r = client.post("/ingest", json=body)
    assert r.status_code == 200
    res = r.json()
    assert res["stored"] == 3 and res["rejected"] == 3
    reasons = {x["index"]: x["reason"] for x in res["reasons"]}
    assert "VI-NOPE" in reasons[3]
    assert "ts" in reasons[4]
    assert "equip_id" in reasons[5]


def test_out_of_order_rows_do_not_move_current_backwards(client):
    _post(client, _batch())
    late = _batch(batch_id="b-late", samples=[
        Sample(equip_id="C-1101", tag_id="VI-C1101", ts=f"2026-11-08T17:22:50{KST}", value=9.9, quality="Good"),
    ])
    assert _post(client, late).json()["stored"] == 1
    cur = client.get("/tags/VI-C1101/current").json()
    assert cur["value"] == 2.36


def test_unsupported_schema_version_rejected(client):
    r = _post(client, _batch(), schema_version="2.0")
    assert r.status_code == 400


def test_health_reports_last_received(client):
    assert client.get("/health").json()["last_received_at"] is None
    _post(client, _batch())
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["last_received_at"] is not None
    assert h["sim_now"].startswith("2026-11-08T08:23:20")   # 저장 시각은 UTC


def test_unknown_tag_current_is_404(client):
    assert client.get("/tags/VI-NOPE/current").status_code == 404
