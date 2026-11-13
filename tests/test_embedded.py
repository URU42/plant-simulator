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


def test_restart_restores_generator_from_db(plant_path, runtime_path, tmp_path, monkeypatch):
    """같은 DB 로 다시 띄우면 LocalSender 가 저장소의 태그별 최근 값을 넘겨 생성기를 되살린다."""
    from simulator.nelson_model import NelsonGenerator

    restored = []
    original = NelsonGenerator.restore
    monkeypatch.setattr(NelsonGenerator, "restore", lambda self, v: (restored.append(v), original(self, v)))
    settings = Settings(with_simulators=True, time_mode="live")
    with TestClient(create_single_app(plant_path, runtime_path, tmp_path / "rhds.db", settings=settings)) as c:
        _wait(c, "/plant/health", lambda r: r["sim_now"] is not None)
    assert restored == []                                   # 빈 DB 에서는 되살릴 값이 없다
    with TestClient(create_single_app(plant_path, runtime_path, tmp_path / "rhds.db", settings=settings)) as c:
        _wait(c, "/plant/health", lambda r: bool(restored))
        assert c.get("/plant/health").json()["simulator_error"] is None
    assert len(restored) == 1 and all(1 <= len(v) <= 14 for v in restored[0].values())
