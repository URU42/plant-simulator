"""통합 실행 (k8s 설계 §4.1): 수집 서버 프로세스 안에서 공장 생성기가 돈다."""
import time

import pytest
from fastapi.testclient import TestClient

from collector.app import create_single_app
from collector.settings import Settings


def _wait(client, path, pred, timeout=15.0):
    t = time.monotonic()
    while time.monotonic() - t < timeout:
        r = client.get(path).json()
        if pred(r):
            return r
        time.sleep(0.2)
    raise AssertionError(f"timeout waiting on {path}")


def test_with_simulators_fills_current_values(rfcc_plant_path, rfcc_runtime_path):
    app = create_single_app(rfcc_plant_path, rfcc_runtime_path, ":memory:", settings=Settings(with_simulators=True, time_mode="live"))
    with TestClient(app) as c:
        rows = _wait(c, "/plant/current", lambda r: sum(x["status"] == "Good" for x in r) >= 60)
        assert len(rows) == 81


def test_with_simulators_backfill_keeps_api_responsive(rfcc_plant_path, rfcc_runtime_path, tmp_path):
    app = create_single_app(rfcc_plant_path, rfcc_runtime_path, tmp_path / "rfcc.db",
                            settings=Settings(with_simulators=True, time_mode="backfill", backfill_days=1))
    with TestClient(app) as c:
        slow = 0.0
        for _ in range(20):                       # 백필이 도는 동안 20번 묻는다
            t = time.monotonic(); assert c.get("/plant/health").status_code == 200
            slow = max(slow, time.monotonic() - t); time.sleep(0.3)
        assert slow < 1.0, slow


def test_sim_only_scenario_is_not_seeded_into_history(rfcc_plant_path, rfcc_runtime_path, tmp_path):
    private = tmp_path / "private"; private.mkdir()
    (private / "scenario_plant.yaml").write_text(
        "events:\n  - { event_id: EV-EVAL-1, equip_id: P-1101A, tag_id: VI-P-1101A, pattern: step, start: 2026-11-01T00:00:00+09:00,"
        " duration: 1h, magnitude: 1.0, failure_id: FL-EVAL-1 }\n")
    settings = Settings(with_simulators=True, time_mode="live", sim_scenario_dir=str(private))
    with TestClient(create_single_app(rfcc_plant_path, rfcc_runtime_path, ":memory:", settings=settings)) as c:
        st = c.app.state.plants["plant"]
        assert st.simulator_scenarios == [str(private / "scenario_plant.yaml")]
        assert st.storage.count("failure_history") == 0                      # 평가용 정답은 DB 에 안 들어간다
        assert c.get("/plant/health").json()["simulator_scenarios"] == st.simulator_scenarios
