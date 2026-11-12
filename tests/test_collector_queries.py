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
    samples = [Sample(equip_id=equip_id, tag_id=tag_id, ts=start + timedelta(seconds=s), value=value, status="Good")
               for s in range(0, minutes * 60, 10)]
    batch = IngestBatch(source_id="SIM", batch_id=f"b-{tag_id}-{start.isoformat()}", sent_at=start, samples=samples)
    assert client.post("/plant/ingest", json=batch.model_dump(mode="json")).status_code == 200


def test_timeseries_after_ingest_and_no_limit_alarms(client):
    _send(client, "PI-D-P3101", "P-3101", T0, 10, 56.0)
    _send(client, "PI-D-P3101", "P-3101", T0 + timedelta(minutes=10), 60, 48.0)
    params = {"equip_id": "P-3101", "tag_id": "PI-D-P3101", "from": T0.isoformat(), "to": (T0 + timedelta(hours=2)).isoformat()}
    ts = client.get("/plant/timeseries", params=params | {"interval": "10m"}).json()
    assert len(ts) == 6 and ts[0]["max"] == 56.0 and ts[1]["avg"] == 48.0   # 60~70분 구간은 아직 미완료
    hourly = client.get("/plant/timeseries", params=params).json()
    assert len(hourly) == 1 and hourly[0]["count"] == 360
    assert client.get("/plant/alarms").json() == []                          # 한계값 알람은 만들지 않는다
    assert client.get("/plant/timeseries", params=params | {"from": "x", "to": "y", "interval": "5m"}).status_code == 400


def test_current_becomes_nodata_after_three_send_intervals(client):
    _send(client, "VI-A-P3101", "P-3101", T0, 1, 2.0)
    _send(client, "VI-A-C3102", "C-3102", T0 + timedelta(minutes=5), 1, 2.0)   # 시뮬레이터 시계가 5분 앞으로
    assert client.get("/plant/equipment/C-3102/tags/VI-A-C3102/current").json()["status"] == "Good"
    assert client.get("/plant/equipment/P-3101/tags/VI-A-P3101/current").json()["status"] == "NoData"
    assert client.get("/plant/equipment/P-3101/tags/TI-B-P3101/current").json()["status"] == "NoData"


def test_large_time_jump_is_caught_up_in_background(client):
    """시뮬레이터 시각이 크게 건너뛰어도 /ingest 는 바로 응답하고, 나머지 구간은 뒤에서 따라잡는다."""
    import time

    _send(client, "VI-A-P3101", "P-3101", T0, 1, 2.0)
    t = time.monotonic()
    _send(client, "VI-A-P3101", "P-3101", T0 + timedelta(days=5), 1, 2.0)
    assert time.monotonic() - t < 3.0
    st = client.app.state.plants["plant"].storage
    for _ in range(200):
        if st.query("SELECT COUNT(DISTINCT bucket_start) FROM ts_10m")[0][0] >= 5 * 144:
            break
        time.sleep(0.05)
    assert st.query("SELECT COUNT(DISTINCT bucket_start) FROM ts_10m")[0][0] == 5 * 144
    assert client.get("/plant/health").json()["catching_up"] is False


def test_history_endpoints_are_gone(client):
    """정비·고장 이력(시나리오에서 적재하던 것)은 지웠다."""
    assert client.get("/plant/maintenance").status_code == 404
    assert client.get("/plant/failures").status_code == 404
