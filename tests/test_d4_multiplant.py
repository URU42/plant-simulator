"""D4: 한 수집기의 다섯 공장 트윈 제어 격리·명령 중복 제거·재시작·정답 비노출."""
from datetime import datetime, timedelta

import yaml
from fastapi.testclient import TestClient

from collector.app import create_app
from collector.plants import load_plants
from collector.settings import Settings
from common.config_models import load_plant, load_runtime
from common.sim_clock import KST, ControlledClock
from simulator.model import PlantGenerator

START = datetime(2026, 11, 24, tzinfo=KST)
KEYS = ["cdu", "hou", "rfcc", "rhds", "vrds"]
H = {"X-Ingest-Token": "local-test"}


def _plants(config_dir, tmp_path):
    entries = []
    for key in KEYS:
        runtime = load_yaml_dict(config_dir / f"runtime_{key}.yaml")
        runtime["physics"] = str(config_dir / f"physics_{key}_twin.yaml")
        runtime.pop("physics_general", None)
        path = tmp_path / f"runtime_{key}.yaml"
        path.write_text(yaml.safe_dump(runtime, allow_unicode=True), encoding="utf-8")
        entries.append({"key": key, "plant": str(config_dir / f"plant_{key}.yaml"), "runtime": str(path),
                        "db": str(tmp_path / f"{key}.db")})
    file = tmp_path / "plants.yaml"
    file.write_text(yaml.safe_dump({"fault_modes": str(config_dir / "fault_modes.yaml"), "plants": entries}), encoding="utf-8")
    return load_plants(file)


def load_yaml_dict(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _open(plants):
    app = create_app(plants, Settings(with_simulators=False, ingest_token="local-test"))
    return app


def _clocks(app):
    for ctx in app.state.plants.values():
        ctx.simulation_clock = ControlledClock(START)


def test_controls_are_isolated_per_plant_and_command_ids_are_scoped(config_dir, tmp_path):
    plants = _plants(config_dir, tmp_path)
    app = _open(plants)
    with TestClient(app) as client:
        _clocks(app)
        state = lambda k: client.get(f"/{k}/simulation").json()
        control = lambda k, body: client.post(f"/{k}/simulation/control", headers=H, json=body)
        assert all(state(k)["phase"] == "ready" for k in KEYS)
        assert control("hou", {"command_id": "same", "action": "start"}).status_code == 200
        assert control("hou", {"command_id": "rate", "action": "speed", "rate": 30}).status_code == 200
        assert state("hou")["phase"] == "running" and state("hou")["rate"] == 30
        assert all(state(k)["phase"] == "ready" and state(k)["rate"] == 1 for k in KEYS if k != "hou")
        # 같은 명령 ID 는 공장마다 따로다. 같은 공장 안에서 다른 본문은 409.
        assert control("rfcc", {"command_id": "same", "action": "start"}).status_code == 200
        assert control("hou", {"command_id": "same", "action": "pause"}).status_code == 409
        first = control("rfcc", {"command_id": "same", "action": "start"})
        assert first.status_code == 200 and state("rfcc")["phase"] == "running"
        # 주입은 그 공장 장부에만 생긴다.
        assert control("hou", {"command_id": "inj", "action": "inject", "equip_id": "P-2101", "mode": "PM-SUCTION-LOSS"}).status_code == 200
        assert [a["equip_id"] for a in client.get("/hou/anomalies/active", headers=H).json()] == ["P-2101"]
        for k in KEYS:
            if k != "hou":
                assert client.get(f"/{k}/anomalies/active", headers=H).json() == []
        # 다른 공장의 설비 ID 는 거절한다.
        assert control("rfcc", {"command_id": "x", "action": "inject", "equip_id": "P-2101", "mode": "PM-SUCTION-LOSS"}).status_code == 404
        # 정상 전환은 그 공장만 (HOU 사건 정지, RFCC 영향 없음)
        assert control("hou", {"command_id": "n", "action": "normal"}).status_code == 200
        assert client.get("/hou/anomalies/active", headers=H).json() == []
        # 관측·SSE·카탈로그에는 사건 ID·정답 분류가 없다. 정답 장부는 토큰이 필요하다.
        catalog = client.get("/hou/simulation/catalog", headers=H).text
        assert "FM-HOU-" not in catalog and '"label"' not in catalog and '"code"' not in catalog
        assert client.get("/hou/anomalies").status_code in (401, 403)
        assert client.get("/hou/simulation/catalog").status_code in (401, 403)
        assert client.post("/hou/simulation/control", json={"command_id": "t", "action": "start"}).status_code in (401, 403)


def test_restart_keeps_measurements_and_resets_clock_and_receipts(config_dir, tmp_path):
    plants = _plants(config_dir, tmp_path)
    app = _open(plants)
    entry = next(e for e in plants.plants if e.key == "hou")
    plant, runtime = load_plant(entry.plant), load_runtime(entry.runtime)
    with TestClient(app) as client:
        _clocks(app)
        samples = PlantGenerator(plant, runtime).generate(START, START + timedelta(seconds=3))
        app.state.plants["hou"].receive_local("restart-test", runtime.source_id, samples)
        assert client.post("/hou/simulation/control", headers=H, json={"command_id": "a", "action": "start"}).status_code == 200
        before = {(r["equip_id"], r["tag_id"]): r for r in client.get("/hou/current").json() if r["ts"]}
    # 같은 DB 로 새 프로세스: 현재값·측정 시각은 유지, 조작 시계는 준비 상태, 명령 영수증은 메모리라 비어 있다.
    app2 = _open(plants)
    with TestClient(app2) as client:
        _clocks(app2)
        after = {(r["equip_id"], r["tag_id"]): r for r in client.get("/hou/current").json() if r["ts"]}
        assert after == before and after
        assert client.get("/hou/simulation").json()["phase"] == "ready"
        # 명령 영수증은 프로세스 메모리에만 있다(문서화한 한계): 재시작 뒤 같은 ID 의 명령을 새로 처리한다.
        assert client.post("/hou/simulation/control", headers=H, json={"command_id": "a", "action": "start"}).status_code == 200
        assert client.get("/hou/simulation").json()["phase"] == "running"
        assert all(client.get(f"/{k}/simulation").json()["phase"] == "ready" for k in KEYS if k != "hou")


def test_sigterm_exits_while_sse_subscribers_stay_connected(config_dir, tmp_path):
    """SSE 구독이 열려 있어도 종료 신호 뒤 제한 시간 안에 프로세스가 끝나 생성기가 DB 에 계속 쓰지 않는다."""
    import os
    import signal
    import socket
    import subprocess
    import sys
    import time
    import urllib.request

    from collector.app import GRACEFUL_SHUTDOWN_SEC

    plants_file = tmp_path / "plants.yaml"
    plants_file.write_text(yaml.safe_dump({"fault_modes": str(config_dir / "fault_modes.yaml"), "plants": [
        {"key": "hou", "plant": str(config_dir / "plant_hou.yaml"), "runtime": str(config_dir / "runtime_hou.yaml"),
         "db": str(tmp_path / "hou.db")}]}), encoding="utf-8")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "INGEST_TOKEN": "local-test"}
    proc = subprocess.Popen([sys.executable, "-m", "collector.app", "--plants", str(plants_file), "--host", "127.0.0.1",
                             "--port", str(port)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    streams = []
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1).close()
                break
            except OSError:
                assert time.monotonic() < deadline and proc.poll() is None, "수집기가 뜨지 않았습니다"
                time.sleep(0.2)
        for _ in range(3):
            streams.append(urllib.request.urlopen(f"http://127.0.0.1:{port}/hou/stream", timeout=5))
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=GRACEFUL_SHUTDOWN_SEC + 10)
        assert time.monotonic() - started < GRACEFUL_SHUTDOWN_SEC + 10
    finally:
        for s in streams:
            s.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_compose_twin_config_runs_all_five_plants_with_physics(config_dir):
    """Compose 트윈 오버레이가 쓰는 plants.twin.yaml 은 다섯 공장 모두 물리 회로 묶음을 켠다(CDU 만 띄우지 않는다)."""
    plants = load_plants(config_dir / "plants.twin.yaml")
    assert [e.key for e in plants.plants] == KEYS
    for entry in plants.plants:
        runtime = load_runtime(entry.runtime)
        assert runtime.physics is not None and runtime.physics.name == f"physics_{entry.key}_twin.yaml"
        base = load_runtime(config_dir / f"runtime_{entry.key}.yaml")
        assert runtime.model_dump(exclude={"physics", "physics_general"}) == base.model_dump(exclude={"physics", "physics_general"})
