"""고장 모드 주입·멈춤·정답지 API. 통합 실행(live, 백필 없음) RHDS 하나짜리 앱으로 확인한다."""
import time

import pytest
from fastapi.testclient import TestClient

from collector.app import create_single_app
from collector.settings import Settings
from common.config_models import ConfigError

TOKEN = {"X-Ingest-Token": "t"}
EVAL = {"X-Eval-Token": "e"}
SURGE = {"equip_id": "C-3101", "mode": "CC-SURGE"}


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
    settings = Settings(with_simulators=True, ingest_token="t", eval_token="e", time_mode="live", backfill_days=0)
    app = create_single_app(plant_path, runtime_path, tmp_path_factory.mktemp("api") / "rhds.db", settings=settings)
    with TestClient(app) as c:
        _wait(c, "/plant/health", lambda r: r["live"] and r["sim_now"] is not None)
        yield c


def test_token_required(client):
    assert client.post("/plant/anomalies", json=SURGE).status_code == 403


def test_answer_key_needs_its_own_token(client, plant_path, runtime_path):
    """관측·조작 토큰으로는 정답지를 못 읽는다. EVAL_TOKEN 이 없으면 닫히고, 두 토큰이 같으면 앱을 만들지 않는다."""
    for headers in ({}, TOKEN, {"X-Eval-Token": "t"}, {"X-Ingest-Token": "e"}):
        assert client.get("/plant/anomalies", headers=headers).status_code == 403
    assert client.get("/plant/anomalies", headers=EVAL).status_code == 200
    assert client.post("/plant/anomalies", json=SURGE, headers=EVAL).status_code == 403      # 정답지 토큰으로 조작할 수 없다
    closed = create_single_app(plant_path, runtime_path, ":memory:", settings=Settings(with_simulators=False, ingest_token="t"))
    with TestClient(closed) as c:
        assert c.get("/plant/anomalies", headers=EVAL).status_code == 403
        assert c.get("/plant/anomalies", headers={"X-Eval-Token": ""}).status_code == 403
    with pytest.raises(ConfigError):
        create_single_app(plant_path, runtime_path, ":memory:", settings=Settings(ingest_token="t", eval_token="t"))


def test_split_mode_conflict(plant_path, runtime_path):
    app = create_single_app(plant_path, runtime_path, ":memory:", settings=Settings(with_simulators=False, ingest_token="t"))
    with TestClient(app) as c:
        r = c.post("/plant/anomalies", json=SURGE, headers=TOKEN)
        assert r.status_code == 409 and r.json()["detail"] == "embedded only"


def test_backfilling_returns_503(client):
    ctx = client.app.state.plants["plant"]
    ctx.live = False
    try:
        r = client.post("/plant/anomalies", json=SURGE, headers=TOKEN)
        assert r.status_code == 503 and r.json()["detail"] == "backfilling"
    finally:
        ctx.live = True


def test_fault_modes_listing(client):
    """걸 수 있는 고장 모드와 대상 설비. 정답 분류는 싣지 않는다."""
    rows = {r["mode"]: r for r in client.get("/plant/fault-modes").json()}
    assert rows["CC-SURGE"]["equip_ids"] == ["C-3101"] and "ST-WATER-INDUCTION" not in rows
    assert rows["RC-VALVE-LEAK"]["equip_ids"] == ["C-3102"] and rows["CC-SURGE"]["unobserved"]
    assert not {"label", "kind", "cause", "expect"} & set(rows["CC-SURGE"])
    assert rows["CC-SURGE"]["basis"].startswith("가정 기반") and rows["RC-VALVE-LEAK"]["basis"] is None      # 모델 종류 표시


def test_flow_graph(client):
    """3D 뷰어가 읽는 공정 흐름 그래프: 공정 노드, 설비와 축 연결, 간선(stream)."""
    body = client.get("/plant/flow").json()
    assert body["plant"] == "PL-RHDS" and body["nodes"]["SP"] == "고압 분리기"
    assert {"equip_id": "GB-3101", "driven_by": "M-3101"} in body["equipment"]
    assert {"from": "C-3102", "to": "C-3101", "label": "보충 수소", "stream": "utility"} in body["edges"]
    assert {e["stream"] for e in body["edges"]} == {"process", "utility"}


def test_inject_busy_stop_answer_key(client):
    """값이 실제로 움직이는지는 생성기·장부 테스트가 본다 (실시간 ramp 는 수십 초가 걸린다)."""
    r = client.post("/plant/anomalies", json=SURGE, headers=TOKEN)
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"event_id", "equip_id", "mode", "speed", "start_at"} and body["speed"] == 1 and body["event_id"].startswith("FM-PL-RHDS-")
    assert client.post("/plant/anomalies", json=SURGE, headers=TOKEN).status_code == 409
    _wait(client, "/plant/health", lambda h: h["sim_now"] > body["start_at"], timeout=8.0)   # 시작 뒤에 멈춰야 정답지에 남는다
    stop = client.post("/plant/anomalies/C-3101/stop", headers=TOKEN).json()
    assert stop["stopped"] == [body["event_id"]] and stop["stop_requested_at"] >= body["start_at"]
    assert client.post("/plant/anomalies/C-3101/stop", headers=TOKEN).status_code == 404
    row = next(x for x in client.get("/plant/anomalies?equip_id=C-3101", headers=EVAL).json() if x["event_id"] == body["event_id"])
    assert row["mode"] == "CC-SURGE" and row["stop_requested_at"] == stop["stop_requested_at"] and row["end_at"] is not None
    assert row["response_tags"] and row["stages"][0]["id"] == "S1"


def test_unknown_equipment_and_mode(client):
    assert client.post("/plant/anomalies", json={"equip_id": "X-9999", "mode": "CC-SURGE"}, headers=TOKEN).status_code == 404
    assert client.post("/plant/anomalies/X-9999/stop", headers=TOKEN).status_code == 404
    for body in ({"equip_id": "C-3101", "mode": "NO-SUCH"}, {"equip_id": "P-3101", "mode": "CC-SURGE"}):
        r = client.post("/plant/anomalies", json=body, headers=TOKEN)
        assert r.status_code == 422, r.text
    assert client.post("/plant/anomalies", json={"equip_id": "C-3101", "rule": 1}, headers=TOKEN).status_code == 422


def test_stop_all_returns_list(client):
    r = client.post("/plant/anomalies/stop", headers=TOKEN)
    assert r.status_code == 200 and isinstance(r.json()["stopped"], list) and r.json()["stop_requested_at"]


def test_dead_simulator_returns_503(client):
    """생성기가 죽었으면 주입·멈춤은 실시간 확인보다 먼저 503 이다."""
    ctx = client.app.state.plants["plant"]
    ctx.simulator_error = "RuntimeError('boom')"
    try:
        for path, body in (("/plant/anomalies", SURGE), ("/plant/anomalies/stop", None)):
            r = client.post(path, json=body, headers=TOKEN)
            assert r.status_code == 503 and r.json()["detail"].startswith("simulator stopped")
    finally:
        ctx.simulator_error = None


TS = {"equip_id": "C-3101", "tag_id": "XI-X-C3101", "to": "2026-11-13T00:00:00Z"}


@pytest.mark.parametrize("path,extra", [("/plant/anomalies", {}), ("/plant/timeseries", TS)])
def test_unparseable_time_is_400(client, path, extra):
    """'+09:00' 을 인코딩하지 않으면 공백이 되어 못 읽는다 → 500 이 아니라 Z·%2B 를 알려 주는 400."""
    r = client.get(path, params=extra | {"from": "2026-11-12T09:00:00 09:00"}, headers=EVAL)
    assert r.status_code == 400 and "from" in r.json()["detail"] and "%2B" in r.json()["detail"]
    assert client.get(path, params=extra | {"from": "2026-11-12T00:00:00Z"}, headers=EVAL).status_code == 200


def test_active_list_follows_inject_and_stop(client):
    """화면의 적용/해제 상태: 안 멈춘 사건만 나온다."""
    body = client.post("/plant/anomalies", json={"equip_id": "P-3101", "mode": "PM-SUCTION-LOSS"}, headers=TOKEN).json()
    active = client.get("/plant/anomalies/active").json()
    assert {"event_id": body["event_id"], "equip_id": "P-3101", "mode": "PM-SUCTION-LOSS", "speed": 1, "start_at": body["start_at"]} in active
    client.post("/plant/anomalies/P-3101/stop", headers=TOKEN)
    assert all(a["equip_id"] != "P-3101" for a in client.get("/plant/anomalies/active").json())


def test_root_page_has_fault_panel(client, plant_path, runtime_path):
    """공장 목록 아래에 고장 설정이 붙는다. 분리 실행이면 안내만 나온다."""
    page = client.get("/").text
    assert 'id="fault-panel"' in page and '"key": "plant"' in page and "__PAGE_DATA__" not in page
    split = create_single_app(plant_path, runtime_path, ":memory:", settings=Settings(with_simulators=False, ingest_token="t"))
    with TestClient(split) as c:
        page = c.get("/").text
        assert 'id="fault-panel"' not in page and "통합 실행" in page



def test_inject_with_speed(client):
    """진행 속도는 1·3·5·10 만. 응답과 적용 중 목록에 실린다."""
    bad = client.post("/plant/anomalies", json={"equip_id": "F-3101", "mode": "FN-IMPELLER-FOULING", "speed": 2}, headers=TOKEN)
    assert bad.status_code == 422
    body = client.post("/plant/anomalies", json={"equip_id": "F-3101", "mode": "FN-IMPELLER-FOULING", "speed": 10}, headers=TOKEN).json()
    assert body["speed"] == 10
    assert any(a["event_id"] == body["event_id"] and a["speed"] == 10 for a in client.get("/plant/anomalies/active").json())
    client.post("/plant/anomalies/F-3101/stop", headers=TOKEN)


def test_repair_after_stop_clears_damage(client):
    """왕복동 밸브 누설은 처음 단계부터 손상이라, 시작 뒤 멈추면 손상이 남고 수리로 고친다."""
    body = client.post("/plant/anomalies", json={"equip_id": "C-3102", "mode": "RC-VALVE-LEAK"}, headers=TOKEN).json()
    assert client.post("/plant/anomalies/C-3102/repair", headers=TOKEN).status_code == 404      # 안 멈췄다
    _wait(client, "/plant/health", lambda h: h["sim_now"] > body["start_at"], timeout=8.0)
    stop = client.post("/plant/anomalies/C-3102/stop", headers=TOKEN).json()
    damaged = client.get("/plant/anomalies/damaged").json()
    assert {"event_id": body["event_id"], "equip_id": "C-3102", "mode": "RC-VALVE-LEAK", "start_at": body["start_at"],
            "stop_requested_at": stop["stop_requested_at"]} in damaged
    assert client.post("/plant/anomalies/C-3102/repair").status_code == 403
    r = client.post("/plant/anomalies/C-3102/repair", headers=TOKEN)
    assert r.status_code == 200 and r.json()["repaired"] == [body["event_id"]] and r.json()["repair_requested_at"] >= stop["stop_requested_at"]
    assert all(d["equip_id"] != "C-3102" for d in client.get("/plant/anomalies/damaged").json())
    assert client.post("/plant/anomalies/C-3102/repair", headers=TOKEN).status_code == 404
    assert client.post("/plant/anomalies/X-9999/repair", headers=TOKEN).status_code == 404
    row = next(x for x in client.get("/plant/anomalies?equip_id=C-3102", headers=EVAL).json() if x["event_id"] == body["event_id"])
    assert row["damage_remains"] and row["repair_requested_at"] == r.json()["repair_requested_at"] and row["end_at"] > row["repair_requested_at"]
    stages = next(m for m in client.get("/plant/fault-modes").json() if m["mode"] == "RC-VALVE-LEAK")["stages"]
    assert [s["damage"] for s in stages] == [True, True]
