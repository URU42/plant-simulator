"""D0 CDU 디지털 트윈 완성 기준선: 회로 묶음(P-8101A→P-8401A 결합 회로, P-8303A NPSH·캐비테이션, F-8201 팬).

물리값(회로 해)과 계측값(생성기 표본)을 분리해 검사한다. 모든 수치는 synthetic assumption 이다.
"""
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from collector.anomaly_book import AnomalyBook, CircuitBusy
from collector.app import create_single_app
from collector.seed_master import seed_master
from collector.settings import Settings
from collector.storage import Storage
from collector.twin_catalog import build_catalog
from common.config_models import ConfigError, load_plant, load_runtime, load_yaml
from common.sim_clock import KST, ControlledClock
from simulator.fault import load_fault_modes
from simulator.model import PlantGenerator
from simulator.physics_adapter import PhysicsBundle, load_physics
from simulator.physics_single import SingleAdapter, SingleCase

START = datetime(2026, 11, 24, tzinfo=KST)
EPOCH = int(START.timestamp())
# (설비, 모드) → (회로 solve 인자, 손상 여부, 정답 코드)
SCENARIOS = {
    ("P-8101A", "PM-SUCTION-LOSS"): ((1, 0, 0, 0, 0), False, 0),
    ("P-8101A", "PM-IMPELLER-WEAR"): ((0, 1, 0, 0, 0), True, 0),
    ("P-8101A", "PM-DISCHARGE-VALVE-RESTRICTION"): ((0, 0, 1, 0, 0), False, 0),
    ("P-8101A", "OP-THROUGHPUT-CHANGE"): ((0, 0, 0.2, 0, 0), False, 1),
    ("P-8101B", "PM-SUCTION-LOSS"): ((0, 0, 0, 0, 0, 1, 0), False, 0),
    ("P-8101B", "PM-IMPELLER-WEAR"): ((0, 0, 0, 0, 0, 0, 1), True, 0),
    ("P-8401A", "PM-SUCTION-LOSS"): ((0, 0, 0, 1, 0), False, 0),
    ("P-8401A", "PM-IMPELLER-WEAR"): ((0, 0, 0, 0, 1), True, 0),
    ("P-8303A", "PM-SUCTION-LOSS"): ((1, 0, 0), False, 0),
    ("P-8303A", "PM-SUCTION-HOT"): ((0.55, 0, 1), False, 0),
    ("P-8303A", "PM-IMPELLER-WEAR"): ((0, 1, 0), True, 0),
    ("F-8201", "FN-IMPELLER-FOULING"): ((0, 1, 0), True, 0),
    ("F-8201", "FN-INLET-RESTRICTION"): ((1, 0, 0), False, 0),
}
CIRCUIT_EQUIPMENT = {"P-8101A", "P-8101B", "P-8401A", "P-8303A", "F-8201"}


@pytest.fixture
def cdu(config_dir):
    plant = load_plant(config_dir / "plant_cdu.yaml")
    runtime = load_runtime(config_dir / "runtime_cdu.yaml").model_copy(update={"physics": config_dir / "physics_cdu_twin.yaml", "physics_general": False})
    physics = load_physics(plant, runtime.physics)
    storage = Storage(":memory:")
    seed_master(storage, plant)
    book = AnomalyBook(storage, plant, load_fault_modes(config_dir / "fault_modes.yaml"), physics=physics)
    try:
        yield plant, runtime, physics, book
    finally:
        storage.close()


# --- 회로 묶음과 정상 운전점 -----------------------------------------------------------
def test_bundle_circuits_ids_and_exclusions(cdu):
    plant, _, physics, book = cdu
    assert isinstance(physics, PhysicsBundle)
    assert [c["id"] for c in physics.circuits()] == ["coupled:P-8101A", "single:P-8303A", "single:F-8201"]
    assert set(physics.roles) == CIRCUIT_EQUIPMENT
    assert set(physics.exclusions) == {"P-8102A", "P-8301A", "P-8302A"}
    assert {e for e, _ in physics.tag_keys} == CIRCUIT_EQUIPMENT
    catalog = build_catalog(plant, book, physics, EPOCH)
    supported = {(a["equip_id"], s["mode"]) for a in catalog["equipment"] for s in a["scenarios"] if s["supported"]}
    assert supported == set(SCENARIOS)
    for asset in catalog["equipment"]:
        if asset["equip_id"] in physics.exclusions:
            assert asset["reason"] == physics.exclusions[asset["equip_id"]]
            assert all(s["reason"] == asset["reason"] for s in asset["scenarios"])


def test_normal_operating_point_matches_master(cdu):
    plant, _, physics, _ = cdu
    control = plant.control_by_key()
    normal = physics.sensor_centers(physics.state(EPOCH, physics.parameter_plans([])))
    assert set(normal) == physics.tag_keys
    for tag, value in normal.items():
        mean, sigma = control[tag]
        assert abs(value - mean) < 0.01 * sigma + 2e-3, tag


# --- NPSH·캐비테이션 단위·경계·민감도 -----------------------------------------------------
@pytest.fixture
def pump(config_dir):
    return SingleCase.model_validate(load_yaml(config_dir / "physics_cdu_p8303a.yaml"))


def test_cavitation_curve_continuity_and_bounds(pump):
    n = pump.npsh
    assert n.head_loss(n.onset_margin) == 0 and n.head_loss(5) == 0
    assert n.head_loss(1.0) == pytest.approx(0.03)          # NPSHr 정의: 양정 3 % 저하
    eps = 1e-6
    for s in (n.onset_margin, 1.0):                           # 값·기울기 연속
        left, right = n.head_loss(s - eps), n.head_loss(s + eps)
        assert left == pytest.approx(right, abs=1e-6)
        slope_l = (n.head_loss(s - eps) - n.head_loss(s - 2 * eps)) / eps
        slope_r = (n.head_loss(s + 2 * eps) - n.head_loss(s + eps)) / eps
        assert slope_l == pytest.approx(slope_r, abs=1e-3)
    assert n.head_loss(-10) == n.max_head_loss
    values = [n.head_loss(s / 100) for s in range(0, 200)]
    assert values == sorted(values, reverse=True)


def test_vapor_pressure_and_npsh_sensitivity(pump):
    n = pump.npsh
    assert n.vapor_pressure(n.fluid_temp_k) == pytest.approx(n.vapor_pressure_pa)
    assert n.vapor_pressure(n.fluid_temp_k + 15) > n.vapor_pressure(n.fluid_temp_k + 5) > n.vapor_pressure_pa
    normal = pump.solve(0, 0, 0)
    assert normal.npsha_m == pytest.approx(8.0, abs=1e-6) and normal.npshr_m == pytest.approx(4.0, abs=1e-6)
    assert normal.head_loss == 0 and abs(normal.pressure_residual_pa) < 1e-6 and abs(normal.energy_residual_w) < 1e-3
    temps = [pump.solve(0, 0, t / 4) for t in range(5)]
    assert [r.npsha_m for r in temps[1:]] == sorted([r.npsha_m for r in temps[1:]], reverse=True)
    assert [r.flow_m3_s for r in temps] == sorted([r.flow_m3_s for r in temps], reverse=True)
    clogs = [pump.solve(c / 4, 0, 0) for c in range(5)]
    assert [r.inlet_abs_pa for r in clogs] == sorted([r.inlet_abs_pa for r in clogs], reverse=True)
    assert clogs[-1].head_loss > 0.1 and clogs[0].head_loss == 0
    # 회전수 상사: 속도 10 % 감소 시 NPSHr 감소, 캐비테이션 여유 증가
    slow = pump.model_copy(update={"machine": pump.machine.model_copy(update={"speed_rpm": pump.machine.speed_rpm * 0.9})})
    assert slow.solve(0, 0, 1).npsh_margin > pump.solve(0, 0, 1).npsh_margin


def test_cavitation_model_rejects_scenarios_outside_validity(config_dir, tmp_path):
    data = load_yaml(config_dir / "physics_cdu_p8303a.yaml")
    data["max_strainer_resistance_pa_s2_m6"] *= 4
    path = tmp_path / "bad.yaml"
    import yaml
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="유효 범위"):
        SingleAdapter(load_plant(config_dir / "plant_cdu.yaml"), path)


def test_fan_circuit_units_and_curve(config_dir):
    fan = SingleCase.model_validate(load_yaml(config_dir / "physics_cdu_f8201.yaml"))
    normal, inlet, fouled = fan.solve(0, 0, 0), fan.solve(1, 0, 0), fan.solve(0, 1, 0)
    assert normal.rise_pa / 9.80665 == pytest.approx(184.606, abs=0.01)
    assert normal.flow_m3_s * fan.density_kg_m3 / fan.flow_reference_density_kg_m3 * 3600 == pytest.approx(119077.14, abs=1)
    assert inlet.flow_m3_s < normal.flow_m3_s and inlet.rise_pa > normal.rise_pa      # 흡입 저항 → 운전점이 곡선 위쪽으로
    assert fouled.flow_m3_s < normal.flow_m3_s and fouled.rise_pa < normal.rise_pa    # 곡선 자체가 내려감
    assert all(abs(r.pressure_residual_pa) < 1e-6 for r in (normal, inlet, fouled))


# --- 시나리오별 생성기 회귀 -----------------------------------------------------------
@pytest.mark.parametrize(("equip", "mode"), sorted(SCENARIOS))
def test_each_scenario_injects_rises_stops_repairs_and_recovers(cdu, config_dir, equip, mode):
    plant, runtime, physics, book = cdu
    args, damage, code = SCENARIOS[(equip, mode)]
    event = book.inject(equip, mode, EPOCH + 10, 10)
    assert event.mode.label.code == code
    book.stop(equip, EPOCH + 310)
    if damage:
        assert book.repair(equip, EPOCH + 400)[1] == [event.event_id]
    plans = physics.parameter_plans(book.external(EPOCH, EPOCH + 650))
    owner = physics._owner[equip]
    index = physics.adapters.index(owner)
    normal_states = physics.state(EPOCH, physics.parameter_plans([]))
    states = {t: physics.state(EPOCH + t, plans) for t in (11, 25, 300, 380, 560, 640)}
    assert states[300][index] == owner.solve(*args), "최대 단계 도달 후 회로 해"
    assert states[25][index] not in (normal_states[index], states[300][index]), "단계 상승 중"
    assert (states[380][index] == states[300][index]) == damage, "정지 뒤 손상 유지 여부"
    slow = mode == "PM-SUCTION-HOT"                               # 흡입 온도 파라미터는 300초 동안 회복한다
    assert (states[560][index] == normal_states[index]) != slow and states[640][index] == normal_states[index], "회복"
    for i, state in enumerate(states[300]):                       # 다른 회로는 다시 풀지 않는다
        if i != index:
            assert state == normal_states[i]
    # 생성기 표본: 물리 태그는 해의 중심값 변화만큼, 경험 효과를 중복하지 않는다.
    window = lambda gen, s: gen.generate(START + timedelta(seconds=s - 1), START + timedelta(seconds=s + 1))
    at = lambda samples, s: {(x.equip_id, x.tag_id): x.value for x in samples if x.ts.timestamp() == EPOCH + s}
    generated = at(window(PlantGenerator(plant, runtime, book), 300), 300)
    nominal = at(window(PlantGenerator(plant, runtime), 300), 300)
    decimals = {(e.equip_id, t.tag_id): t.decimals for e in plant.equipment for t in e.tags}
    centers, reference = owner.sensor_centers(states[300][index]), owner.sensor_centers(normal_states[index])
    moved = 0
    for tag in owner.tag_keys:
        delta = centers[tag] - reference[tag]
        assert generated[tag] - nominal[tag] == pytest.approx(delta, abs=1.001 * 10 ** -decimals[tag]), tag
        moved += abs(delta) > 10 ** -decimals[tag]
    assert moved >= 2, "시나리오가 물리 태그를 움직여야 한다"
    for key in physics.tag_keys - owner.tag_keys:                  # 다른 회로 물리 태그는 그대로
        assert generated[key] == nominal[key], key
    recovered = at(window(PlantGenerator(plant, runtime, book), 650), 650)
    base = at(window(PlantGenerator(plant, runtime), 650), 650)
    assert all(recovered[k] == base[k] for k in physics.tag_keys)


def test_temperature_grid_seed_utc_and_dropout_are_preserved(cdu):
    plant, runtime, physics, book = cdu
    book.inject("P-8303A", "PM-SUCTION-HOT", EPOCH + 5, 10)
    a = PlantGenerator(plant, runtime, book).generate(START, START + timedelta(seconds=40))
    b = PlantGenerator(plant, runtime, book).generate(START, START + timedelta(seconds=40))
    assert a == b                                                   # 고정 seed
    assert all(s.ts.utcoffset() is not None for s in a)
    temps = [s for s in a if s.tag_id.startswith("TI-")]
    assert temps and all(int(s.ts.timestamp()) % 10 == 0 for s in temps)
    assert sum(s.tag_id == "FI-8303A" for s in a) == 40
    from simulator.instrument import InstrumentEvent
    gen = PlantGenerator(plant, runtime, book)
    gen.instrument = type("Fixed", (), {"events": lambda self, s, e: [
        InstrumentEvent("evaluation-only", "dropout", "P-8303A", ("FI-8303A",), EPOCH + 3, EPOCH + 6)]})()
    dropped = gen.generate(START, START + timedelta(seconds=40))
    assert dropped == [s for s in a if not (s.tag_id == "FI-8303A" and EPOCH + 3 <= s.ts.timestamp() < EPOCH + 6)]


def test_circuits_are_isolated_for_concurrency(cdu):
    _, _, _, book = cdu
    book.inject("P-8303A", "PM-SUCTION-LOSS", EPOCH + 10)
    book.inject("F-8201", "FN-INLET-RESTRICTION", EPOCH + 11)        # 다른 회로는 동시에 가능
    book.inject("P-8101A", "PM-IMPELLER-WEAR", EPOCH + 12)
    with pytest.raises(CircuitBusy):
        book.inject("P-8401A", "PM-SUCTION-LOSS", EPOCH + 13)        # 같은 결합 회로는 거절


def test_cdu_control_api_with_bundle(config_dir, tmp_path):
    path = tmp_path / "runtime.yaml"
    runtime = load_runtime(config_dir / "runtime_cdu.yaml").model_copy(update={"physics": config_dir / "physics_cdu_twin.yaml", "physics_general": False})
    path.write_text(runtime.model_dump_json(), encoding="utf-8")
    app = create_single_app(config_dir / "plant_cdu.yaml", path, ":memory:",
                            settings=Settings(with_simulators=False, ingest_token="local-test"))
    headers = {"X-Ingest-Token": "local-test"}
    with TestClient(app) as client:
        app.state.plants["plant"].simulation_clock = ControlledClock(START)
        listing = client.get("/plant/equipment").json()
        assert len(listing) == 8 and sum(len(a["tags"]) for a in listing) == 67
        models = {t["measurement_model"] for a in listing for t in a["tags"]}
        assert models == {"physical", "empirical"}
        assert {a["equip_id"] for a in listing for t in a["tags"] if t["circuit_id"]} == CIRCUIT_EQUIPMENT
        catalog = client.get("/plant/simulation/catalog", headers=headers).json()
        assert [c["id"] for c in catalog["circuits"]] == ["coupled:P-8101A", "single:P-8303A", "single:F-8201"]

        def control(n, **body):
            return client.post("/plant/simulation/control", headers=headers, json={"command_id": f"c{n}", **body})

        assert control(1, action="start").status_code == 200
        rejected = control(2, action="inject", equip_id="P-8102A", mode="PM-SUCTION-LOSS")
        assert rejected.status_code == 422 and "세정수" in rejected.json()["detail"]
        assert control(3, action="inject", equip_id="P-8303A", mode="PM-SUCTION-HOT").status_code == 200
        assert control(4, action="inject", equip_id="F-8201", mode="FN-IMPELLER-FOULING").status_code == 200
        assert control(3, action="inject", equip_id="P-8303A", mode="PM-SUCTION-HOT").status_code == 200  # 같은 명령 재시도
        assert control(3, action="pause").status_code == 409                                            # ID 재사용
        assert control(5, action="normal").status_code == 200
        state = client.get("/plant/simulation/catalog", headers=headers).json()
        assert all(a["state"] != "active" for a in state["equipment"])
        assert next(a for a in state["equipment"] if a["equip_id"] == "F-8201")["state"] in ("recovering", "idle")
