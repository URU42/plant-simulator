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
    stop = client.post("/plant/anomalies/C-3101/stop", headers=TOKEN).json()
    assert stop["stopped"] == [body["event_id"]] and stop["stop_requested_at"] >= body["start_at"]
    assert client.post("/plant/anomalies/C-3101/stop", headers=TOKEN).status_code == 404
    row = next(x for x in client.get("/plant/anomalies?equip_id=C-3101").json() if x["event_id"] == body["event_id"])
    assert row["origin"] == "injected" and row["stop_requested_at"] == stop["stop_requested_at"] and row["end_at"] is not None


def test_unknown_equipment_and_bad_rule(client):
    assert client.post("/plant/anomalies", json={"equip_id": "X-9999", "rule": 1}, headers=TOKEN).status_code == 404
    assert client.post("/plant/anomalies/X-9999/stop", headers=TOKEN).status_code == 404
    assert client.post("/plant/anomalies", json={"equip_id": "C-3101", "rule": 9}, headers=TOKEN).status_code == 422


def test_stop_all_returns_list(client):
    r = client.post("/plant/anomalies/stop", headers=TOKEN)
    assert r.status_code == 200 and isinstance(r.json()["stopped"], list) and r.json()["stop_requested_at"]


def test_env_overrides_interval(monkeypatch):
    monkeypatch.setenv("PERSISTENT_FAULT_INTERVAL_MIN", "0")
    assert Settings.from_env().persistent_fault_interval_min == 0


def test_dead_simulator_returns_503(client):
    """생성기가 죽었으면 주입·멈춤은 실시간 확인보다 먼저 503 이다."""
    ctx = client.app.state.plants["plant"]
    ctx.simulator_error = "RuntimeError('boom')"
    try:
        for path, body in (("/plant/anomalies", {"equip_id": "C-3101", "rule": 1}), ("/plant/anomalies/stop", None)):
            r = client.post(path, json=body, headers=TOKEN)
            assert r.status_code == 503 and r.json()["detail"].startswith("simulator stopped")
    finally:
        ctx.simulator_error = None


TS = {"equip_id": "C-3101", "tag_id": "XI-X-C3101", "to": "2026-11-13T00:00:00Z"}


@pytest.mark.parametrize("path,extra", [("/plant/alarms", {}), ("/plant/anomalies", {}), ("/plant/timeseries", TS)])
def test_unparseable_time_is_400(client, path, extra):
    """'+09:00' 을 인코딩하지 않으면 공백이 되어 못 읽는다 → 500 이 아니라 Z·%2B 를 알려 주는 400."""
    r = client.get(path, params=extra | {"from": "2026-11-12T09:00:00 09:00"})
    assert r.status_code == 400 and "from" in r.json()["detail"] and "%2B" in r.json()["detail"]
    assert client.get(path, params=extra | {"from": "2026-11-12T00:00:00Z"}).status_code == 200
