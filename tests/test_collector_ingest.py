"""수집 서버 (기획서 6.2, 7.1, 9.2 4단계): /ingest, ts_raw·tag_current 저장, 설비 마스터 적재."""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from collector.app import create_single_app
from common.message import IngestBatch, Sample

KST = "+09:00"


@pytest.fixture
def client(plant_path, runtime_path):
    app = create_single_app(plant_path, runtime_path, ":memory:")
    with TestClient(app) as c:
        yield c


def _batch(batch_id="b-20261108-172400-SIM-ALL-0001", samples=None):
    samples = samples or [
        Sample(equip_id="C-3101", tag_id="XI-X-C3101", ts=f"2026-11-08T17:23:10{KST}", value=2.34),
        Sample(equip_id="C-3101", tag_id="XI-X-C3101", ts=f"2026-11-08T17:23:20{KST}", value=2.36),
        Sample(equip_id="P-3101", tag_id="TI-B-P3101", ts=f"2026-11-08T17:23:10{KST}", value=65.0),
    ]
    return IngestBatch(source_id="SIM-ALL", batch_id=batch_id, sent_at=f"2026-11-08T17:24:00{KST}", samples=samples)


def _post(client, batch: IngestBatch, **overrides):
    body = batch.model_dump(mode="json") | overrides
    return client.post("/plant/ingest", json=body)


def test_master_is_seeded_from_plant_config(client):
    storage = client.app.state.plants["plant"].storage
    assert storage.count("equipment") == 9
    assert storage.count("equipment_tag") == 58
    assert storage.count("unit") == 6
    assert storage.count("equipment_type") == 5
    assert storage.count("tag_std") == 16
    row = storage.query_dicts("SELECT * FROM equipment_tag WHERE equip_id = 'P-3101' AND tag_id = 'VI-A-P3101'")[0]
    assert (row["control_mean"], row["control_sigma"]) == (1.65, 0.04614035877985053)
    assert (row["warn_hi"], row["alarm_hi"], row["trip_hi"], row["interval_sec"], row["decimals"]) == (4.5, 7.1, 11, 1, 3)
    assert "delay_samples" not in row and "location" not in row


def test_ingest_stores_rows_and_updates_current(client):
    r = _post(client, _batch())
    assert r.status_code == 200
    body = r.json()
    assert body["stored"] == 3 and body["rejected"] == 0 and body["duplicate"] is False
    assert client.app.state.plants["plant"].storage.count("ts_raw") == 3

    cur = client.get("/plant/equipment/C-3101/tags/XI-X-C3101/current").json()
    assert cur["value"] == 2.36 and "status" not in cur
    assert cur["ts"] == "2026-11-08T08:23:20+00:00"   # 저장 시각은 UTC


def test_sensor_series_uses_collected_samples_and_keeps_millisecond_bounds(client):
    assert _post(client, _batch()).status_code == 200
    params = {"equip_id": "C-3101", "tag_id": "XI-X-C3101", "from": "2026-11-08T17:23:10.001+09:00",
              "to": "2026-11-08T08:23:20Z", "aggregation": "raw", "max_points": 1}
    series = client.get("/plant/sensor-series", params=params)
    at = int(datetime.fromisoformat("2026-11-08T08:23:20+00:00").timestamp() * 1000)
    assert series.json() == {"aggregation": "raw", "source_rows": 1, "points": [{"timestamp": at, "value": 2.36}]}
    params["from"] = "2026-11-08T08:23:10Z"
    assert client.get("/plant/sensor-series", params=params).status_code == 413
    body = client.get("/plant/sensor-series", params=params | {"aggregation": "time_bucket"}).json()
    assert body["source_rows"] == 2
    assert body["points"][0] == {"timestamp": at - 10_000, "count": 2, "valid_count": 2, "missing_count": 0,
                                 "mean": pytest.approx(2.35), "min": 2.34, "max": 2.36}


def test_sensor_series_combines_expired_rollups_with_recent_raw_data(client):
    assert _post(client, _batch()).status_code == 200
    ctx = client.app.state.plants["plant"]
    ctx.storage.execute("INSERT INTO ts_10m VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        ("C-3101", "XI-X-C3101", "2026-11-08T08:10:00+00:00", 4.0, 2.0, 6.0, 1.0, 3))
    cutover = datetime.fromisoformat("2026-11-08T08:20:00+00:00")
    cursor = (cutover + timedelta(days=ctx.runtime.raw_retention_days)).isoformat()
    ctx.storage.execute("INSERT OR REPLACE INTO job_state VALUES ('10m', ?)", (cursor,))
    params = {"equip_id": "C-3101", "tag_id": "XI-X-C3101", "from": "2026-11-08T08:10:00Z",
              "to": "2026-11-08T08:23:20Z", "max_points": 1}
    assert client.get("/plant/sensor-series", params=params | {"aggregation": "raw"}).status_code == 413
    body = client.get("/plant/sensor-series", params=params | {"max_points": 10}).json()
    assert body["source_rows"] == 5
    assert body["max_points"] == 1
    assert body["points"][0]["mean"] == pytest.approx((12 + 2.34 + 2.36) / 5)
    assert body["points"][0]["max"] == 6.0
    assert client.get("/plant/sensor-series", params=params | {"tag_id": "unknown"}).status_code == 404


def test_duplicate_batch_is_stored_once(client):
    _post(client, _batch())
    r = _post(client, _batch())
    assert r.json()["duplicate"] is True and r.json()["stored"] == 0
    assert client.app.state.plants["plant"].storage.count("ts_raw") == 3


def test_same_tag_ts_in_another_batch_is_stored_once(client):
    _post(client, _batch())
    r = _post(client, _batch(batch_id="b-20261108-172500-SIM-ALL-0002"))
    assert r.json()["stored"] == 0 and r.json()["duplicate"] is False
    assert client.app.state.plants["plant"].storage.count("ts_raw") == 3


def test_unknown_tag_and_malformed_rows_are_rejected_with_reason(client):
    body = _batch().model_dump(mode="json")
    body["samples"].append({"equip_id": "C-3101", "tag_id": "VI-NOPE", "ts": f"2026-11-08T17:23:10{KST}", "value": 1.0})
    body["samples"].append({"equip_id": "C-3101", "tag_id": "XI-X-C3101", "ts": "not-a-time", "value": 1.0})
    body["samples"].append({"equip_id": "P-3101", "tag_id": "XI-X-C3101", "ts": f"2026-11-08T17:23:30{KST}", "value": 1.0})
    r = client.post("/plant/ingest", json=body)
    assert r.status_code == 200
    res = r.json()
    assert res["stored"] == 3 and res["rejected"] == 3
    reasons = {x["index"]: x["reason"] for x in res["reasons"]}
    assert "VI-NOPE" in reasons[3]
    assert "ts" in reasons[4]
    assert "P-3101/XI-X-C3101" in reasons[5]          # 그 설비에는 그 태그가 없다


def test_out_of_order_rows_do_not_move_current_backwards(client):
    _post(client, _batch())
    late = _batch(batch_id="b-late", samples=[
        Sample(equip_id="C-3101", tag_id="XI-X-C3101", ts=f"2026-11-08T17:22:50{KST}", value=9.9),
    ])
    assert _post(client, late).json()["stored"] == 1
    cur = client.get("/plant/equipment/C-3101/tags/XI-X-C3101/current").json()
    assert cur["value"] == 2.36


def test_unsupported_schema_version_rejected(client):
    for version in ("2.0", "4.0"):              # v2 는 status 칸이 있던 형식 (2026-11-19 에 뺐다)
        assert _post(client, _batch(), schema_version=version).status_code == 400


def test_health_reports_last_received(client):
    assert client.get("/plant/health").json()["last_received_at"] is None
    _post(client, _batch())
    h = client.get("/plant/health").json()
    assert h["status"] == "ok" and h["last_received_at"] is not None
    assert h["sim_now"].startswith("2026-11-08T08:23:20")   # 저장 시각은 UTC


def test_unknown_tag_current_is_404(client):
    assert client.get("/plant/equipment/X-9/tags/VI-NOPE/current").status_code == 404


def test_sample_with_added_field_is_accepted_v1x(client):
    """v1.x 는 필드 추가만 허용 — 추가 필드가 있는 행도 받는다 (6.3)."""
    body = _batch().model_dump(mode="json")
    body["samples"][0]["new_field"] = "x"
    r = client.post("/plant/ingest", json=body)
    assert r.json()["stored"] == 3 and r.json()["rejected"] == 0


def test_rows_older_than_retention_are_rejected(client):
    _post(client, _batch())
    old = _batch(batch_id="b-old", samples=[
        Sample(equip_id="C-3101", tag_id="XI-X-C3101", ts=f"2026-10-01T00:00:00{KST}", value=2.0)])
    res = _post(client, old).json()
    assert res["stored"] == 0 and res["rejected"] == 1 and "retention" in res["reasons"][0]["reason"]


def test_v1_batch_is_rejected(client):
    assert _post(client, _batch(), schema_version="1.0").status_code == 400
