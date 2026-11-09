"""조회 API (기획서 6.4): /timeseries, /alarms, 현재값 NoData."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from collector.app import create_single_app
from common.message import IngestBatch, Sample

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def client(plant_path, runtime_path):
    with TestClient(create_single_app(plant_path, runtime_path, ":memory:")) as c:
        yield c


def _send(client, tag_id, equip_id, start, minutes, value):
    samples = [Sample(equip_id=equip_id, tag_id=tag_id, ts=start + timedelta(seconds=s), value=value, quality="Good")
               for s in range(0, minutes * 60, 10)]
    batch = IngestBatch(source_id="SIM", batch_id=f"b-{tag_id}-{start.isoformat()}", sent_at=start, samples=samples)
    assert client.post("/plant/ingest", json=batch.model_dump(mode="json")).status_code == 200


def test_timeseries_and_alarms_after_ingest(client):
    _send(client, "PI-C1101", "C-1101", T0, 10, 56.0)        # warn 55, delay 1 → Soft/Low
    _send(client, "PI-C1101", "C-1101", T0 + timedelta(minutes=10), 60, 48.0)
    ts = client.get("/plant/timeseries", params={"equip_id": "C-1101", "tag_id": "PI-C1101", "from": T0.isoformat(), "to": (T0 + timedelta(hours=2)).isoformat(),
                                           "interval": "10m"}).json()
    assert len(ts) == 6 and ts[0]["max"] == 56.0   # 60~70분 구간은 아직 미완료 and ts[1]["avg"] == 48.0
    hourly = client.get("/plant/timeseries", params={"equip_id": "C-1101", "tag_id": "PI-C1101", "from": T0.isoformat(), "to": (T0 + timedelta(hours=2)).isoformat()}).json()
    assert len(hourly) == 1 and hourly[0]["count"] == 360
    alarms = client.get("/plant/alarms", params={"equip_id": "C-1101"}).json()
    assert len(alarms) == 1 and alarms[0]["severity"] == "Low" and alarms[0]["cleared_at"] is not None
    assert client.get("/plant/alarms", params={"equip_id": "P-5101"}).json() == []
    assert client.get("/plant/timeseries", params={"equip_id": "C-1101", "tag_id": "PI-C1101", "from": "x", "to": "y", "interval": "5m"}).status_code == 400


def test_current_becomes_nodata_after_three_send_intervals(client):
    _send(client, "VI-C1101", "C-1101", T0, 1, 2.0)
    _send(client, "VI-C2101", "C-2101", T0 + timedelta(minutes=5), 1, 2.0)   # 시뮬레이터 시계가 5분 앞으로
    assert client.get("/plant/equipment/C-2101/tags/VI-C2101/current").json()["status"] == "Good"
    assert client.get("/plant/equipment/C-1101/tags/VI-C1101/current").json()["status"] == "NoData"
    assert client.get("/plant/equipment/C-1101/tags/TI-C1101/current").json()["status"] == "NoData"


def test_large_time_jump_is_caught_up_in_background(client):
    """시뮬레이터 시각이 크게 건너뛰어도 /ingest 는 바로 응답하고, 나머지 구간은 뒤에서 따라잡는다."""
    import time

    _send(client, "VI-C1101", "C-1101", T0, 1, 2.0)
    t = time.monotonic()
    _send(client, "VI-C1101", "C-1101", T0 + timedelta(days=5), 1, 2.0)
    assert time.monotonic() - t < 3.0
    st = client.app.state.plants["plant"].storage
    for _ in range(200):
        if st.query("SELECT COUNT(DISTINCT bucket_start) FROM ts_10m")[0][0] >= 5 * 144:
            break
        time.sleep(0.05)
    assert st.query("SELECT COUNT(DISTINCT bucket_start) FROM ts_10m")[0][0] == 5 * 144
    assert client.get("/plant/health").json()["catching_up"] is False
