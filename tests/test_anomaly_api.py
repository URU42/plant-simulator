"""주입·멈춤·정답지 API (설계 §4.6). 통합 실행(live, 백필 없음) RHDS 하나짜리 앱으로 확인한다."""
import time

import pytest
from fastapi.testclient import TestClient

from collector.app import create_single_app
from collector.settings import Settings

TOKEN = {"X-Ingest-Token": "t"}


def _wait(client, path, pred, timeout=10.0):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        r = client.get(path).json()
        if pred(r):
            return r
        time.sleep(0.2)
    raise AssertionError(f"timeout waiting on {path}")


@pytest.fixture(scope="module")
def client(plant_path, runtime_path, tmp_path_factory):
    """생성기가 실시간으로 도는 통합 실행 앱. 모듈 안 테스트가 함께 쓴다."""
    settings = Settings(with_simulators=True, ingest_token="t", time_mode="live", backfill_days=0)
    app = create_single_app(plant_path, runtime_path, tmp_path_factory.mktemp("api") / "rhds.db", settings=settings)
    with TestClient(app) as c:
        _wait(c, "/plant/health", lambda r: r["live"] and r["sim_now"] is not None)
        yield c


def test_token_required(client):
    assert client.post("/plant/anomalies", json={"equip_id": "C-3101", "rule": 1}).status_code == 403


def test_split_mode_conflict(plant_path, runtime_path):
    app = create_single_app(plant_path, runtime_path, ":memory:", settings=Settings(with_simulators=False, ingest_token="t"))
    with TestClient(app) as c:
        r = c.post("/plant/anomalies", json={"equip_id": "C-3101", "rule": 1}, headers=TOKEN)
        assert r.status_code == 409 and r.json()["detail"] == "embedded only"


def test_backfilling_returns_503(client):
    ctx = client.app.state.plants["plant"]
    ctx.live = False
    try:
        r = client.post("/plant/anomalies", json={"equip_id": "C-3101", "rule": 1}, headers=TOKEN)
        assert r.status_code == 503 and r.json()["detail"] == "backfilling"
    finally:
        ctx.live = True


def test_inject_then_bad_then_stop(client):
    r = client.post("/plant/anomalies", json={"equip_id": "C-3101", "rule": 1}, headers=TOKEN)
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"event_id", "equip_id", "rule", "start_at"} and body["event_id"].startswith("IN-PL-RHDS-")
    _wait(client, "/plant/current", lambda rows: any(
        x["equip_id"] == "C-3101" and x["tag_id"] == "XI-X-C3101" and x["status"] == "Bad" for x in rows), timeout=6.0)
    alarms = _wait(client, "/plant/alarms?equip_id=C-3101&rule=1", bool, timeout=6.0)
    assert not {"kind", "cause", "event_id"} & set(alarms[0])
    assert client.post("/plant/anomalies/C-3101/stop", headers=TOKEN).json() == {"stopped": [body["event_id"]]}
    assert client.post("/plant/anomalies/C-3101/stop", headers=TOKEN).status_code == 404
    row = next(x for x in client.get("/plant/anomalies?equip_id=C-3101").json() if x["event_id"] == body["event_id"])
    assert row["origin"] == "injected" and row["stop_requested_at"] is not None and row["end_at"] is not None


def test_unknown_equipment_and_bad_rule(client):
    assert client.post("/plant/anomalies", json={"equip_id": "X-9999", "rule": 1}, headers=TOKEN).status_code == 404
    assert client.post("/plant/anomalies/X-9999/stop", headers=TOKEN).status_code == 404
    assert client.post("/plant/anomalies", json={"equip_id": "C-3101", "rule": 9}, headers=TOKEN).status_code == 422


def test_stop_all_returns_list(client):
    r = client.post("/plant/anomalies/stop", headers=TOKEN)
    assert r.status_code == 200 and isinstance(r.json()["stopped"], list)


def test_env_overrides_interval(monkeypatch):
    monkeypatch.setenv("PERSISTENT_FAULT_INTERVAL_MIN", "0")
    assert Settings.from_env().persistent_fault_interval_min == 0
