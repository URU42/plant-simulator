"""다섯 플랜트 대표 수력 회로: 정상 운전점 재현, 시나리오별 물리 변화·복귀, 회로당 한 시나리오, 트윈 카탈로그와 조작 API."""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from collector.anomaly_book import AnomalyBook, CircuitBusy
from collector.app import create_single_app
from collector.seed_master import seed_master
from collector.settings import Settings
from collector.storage import Storage
from collector.twin_catalog import OUTSIDE, build_catalog, injectable
from common.config_models import load_plant, load_runtime
from common.sim_clock import KST, ControlledClock
from simulator.fault import load_fault_modes
from simulator.model import PlantGenerator
from simulator.physics_adapter import PhysicsAdapter

START = datetime(2026, 11, 24, tzinfo=KST)
EPOCH = int(START.timestamp())
CIRCUITS = {"cdu": ("P-8101A", "P-8401A", "P-8101B"), "hou": ("P-2101", "P-2701", "P-2101-S2"), "rfcc": ("P-1101A", "P-1303A", "P-1101B"),
            "rhds": ("P-3101", "P-3102", "P-3101-S2"), "vrds": ("P-4101", "P-4102", "P-4101-S2")}      # (첫 입력 펌프, 후속, 둘째 입력 펌프)
# 밸브 제한은 CDU 결합 회로 전용(2026-11-24 D2-A). 다른 공장에는 밸브 설비·태그가 없다.
SOURCE_MODES = {"PM-SUCTION-LOSS", "PM-IMPELLER-WEAR", "OP-THROUGHPUT-CHANGE"}
CDU_SOURCE_EXTRA = {"PM-DISCHARGE-VALVE-RESTRICTION"}
DOWNSTREAM_MODES = {"PM-SUCTION-LOSS", "PM-IMPELLER-WEAR"}
# 흡입 유체 온도 태그가 있는 후속 펌프는 기존 흡입 악화 + 온도 상승 모드도 S3 의 막힘 파라미터로 물리 계산한다.
EXTRA_DOWNSTREAM = {"rhds": {"PM-SUCTION-HOT"}, "vrds": {"PM-SUCTION-HOT"}}
# (설비 역할, 모드) → solve 인자 위치와 최대 크기
SOLVE_INDEX = {("source", "PM-SUCTION-LOSS"): (0, 1.0), ("source", "PM-IMPELLER-WEAR"): (1, 1.0),
               ("source", "PM-DISCHARGE-VALVE-RESTRICTION"): (2, 1.0), ("source", "OP-THROUGHPUT-CHANGE"): (2, 0.2),
               ("downstream", "PM-SUCTION-LOSS"): (3, 1.0), ("downstream", "PM-IMPELLER-WEAR"): (4, 1.0),
               ("downstream", "PM-SUCTION-HOT"): (3, 0.55),
               ("second", "PM-SUCTION-LOSS"): (5, 1.0), ("second", "PM-IMPELLER-WEAR"): (6, 1.0)}


def _setup(config_dir, key):
    plant = load_plant(config_dir / f"plant_{key}.yaml")
    physics = PhysicsAdapter(plant, config_dir / f"physics_{key}_integrated.yaml")
    runtime = load_runtime(config_dir / f"runtime_{key}.yaml").model_copy(update={
        "physics_general": False, "physics": config_dir / f"physics_{key}_integrated.yaml",
        "units": sorted({plant.equipment_by_id()[e].unit_id for e in CIRCUITS[key]}),
    })
    storage = Storage(":memory:")
    seed_master(storage, plant)
    book = AnomalyBook(storage, plant, load_fault_modes(config_dir / "fault_modes.yaml"), physics=physics)
    return plant, runtime, physics, book, storage


@pytest.fixture(params=sorted(CIRCUITS))
def circuit(request, config_dir):
    plant, runtime, physics, book, storage = _setup(config_dir, request.param)
    try:
        yield request.param, plant, runtime, physics, book
    finally:
        storage.close()


def test_normal_solution_reproduces_master_operating_point(circuit):
    key, plant, _, physics, _ = circuit
    assert (physics.primary, physics.downstream, physics.second) == CIRCUITS[key]
    assert physics.case.plant_id == plant.plant.plant_id
    control = plant.control_by_key()
    normal = physics.solve(0, 0, 0)
    assert abs(normal.mass_residual_kg_s) < 1e-9 and abs(normal.pump_pressure_residual_pa) < 1e-4
    for tag, value in physics.sensor_centers(normal).items():
        mean, sigma = control[tag]
        # 정상 해가 관리 기준 μ 와 어긋나면 정상 운전이 편차로 보인다. CDU D2-A 원본은 표시 자릿수만큼의 차이를 허용한다.
        assert abs(value - mean) < 0.01 * sigma + 2e-3, tag


def test_circuit_ids_sensors_and_boundaries_are_plant_specific(config_dir):
    cases = {key: PhysicsAdapter(load_plant(config_dir / f"plant_{key}.yaml"),
                                 config_dir / f"physics_{key}_integrated.yaml") for key in CIRCUITS}
    tags = [tag for adapter in cases.values() for tag in adapter.tag_keys]
    assert len(tags) == len(set(tags))
    boundaries = {(a.case.default_point.suction_abs_pa, a.case.network.density_kg_m3) for a in cases.values()}
    assert len(boundaries) == len(cases)
    for key, adapter in cases.items():
        assert {e for e, _ in adapter.tag_keys} == set(CIRCUITS[key])


@pytest.mark.parametrize("role", ["source", "downstream", "second"])
def test_each_supported_scenario_moves_physics_and_recovers(circuit, role, config_dir):
    key, plant, runtime, physics, _ = circuit
    equip = CIRCUITS[key][["source", "downstream", "second"].index(role)]
    modes = (SOURCE_MODES | (CDU_SOURCE_EXTRA if key == "cdu" else set())) if role == "source" \
        else DOWNSTREAM_MODES if role == "second" else DOWNSTREAM_MODES | EXTRA_DOWNSTREAM.get(key, set())
    normal = physics.solve(0, 0, 0)
    for mode in sorted(modes):
        _, _, _, book, storage = _setup(config_dir, key)
        try:
            event = book.inject(equip, mode, EPOCH + 10, 10)
            assert event.mode.id == mode
            # 10배속: 단계 상승 30~60초, 흡입 악화 S3 는 주입 90초 뒤 시작해 60초 상승 → 170초에 최대
            book.stop(equip, EPOCH + 200)
            damage = mode == "PM-IMPELLER-WEAR"
            if damage:
                assert book.repair(equip, EPOCH + 270)[1] == [event.event_id]
            plans = physics.parameter_plans(book.external(EPOCH, EPOCH + 400))
            index, amount = SOLVE_INDEX[(role, mode)]
            args = [0.0] * 7
            args[index] = amount
            plateau = physics.state(EPOCH + 190, plans)
            assert plateau == physics.solve(*args), (key, mode)
            assert plateau != normal
            assert abs(plateau.mass_residual_kg_s) < 1e-9
            assert all(abs(b.pressure_residual_pa) < 1e-4 for b in plateau.branches)
            assert (physics.state(EPOCH + 265, plans) == plateau) == damage
            assert physics.state(EPOCH + 340, plans) == normal
            generated = PlantGenerator(plant, runtime, book).generate(START + timedelta(seconds=189), START + timedelta(seconds=191))
            nominal = PlantGenerator(plant, runtime).generate(START + timedelta(seconds=189), START + timedelta(seconds=191))
            centers, reference = physics.sensor_centers(plateau), physics.sensor_centers(normal)
            at = {(s.equip_id, s.tag_id): s.value for s in generated if s.ts.timestamp() == EPOCH + 190}
            base = {(s.equip_id, s.tag_id): s.value for s in nominal if s.ts.timestamp() == EPOCH + 190}
            decimals = {(e.equip_id, t.tag_id): t.decimals for e in plant.equipment for t in e.tags}
            for tag in physics.tag_keys:
                # 같은 시드의 잡음은 상쇄되고 표시 자릿수 반올림(두 값 각각 ±0.5 단위)만 남는다.
                step = 10 ** -decimals[tag]
                assert at[tag] - base[tag] == pytest.approx(centers[tag] - reference[tag], abs=step * 1.001), (key, mode, tag)
        finally:
            storage.close()


def test_one_physics_scenario_per_circuit(circuit):
    key, _, _, _, book = circuit
    source, downstream, _ = CIRCUITS[key]
    book.inject(source, "PM-SUCTION-LOSS", EPOCH + 10)
    with pytest.raises(CircuitBusy):
        book.inject(downstream, "PM-IMPELLER-WEAR", EPOCH + 20)
    book.stop(source, EPOCH + 30)
    with pytest.raises(CircuitBusy):          # 회복(60초) 중
        book.inject(downstream, "PM-IMPELLER-WEAR", EPOCH + 60)
    event = book.inject(downstream, "PM-IMPELLER-WEAR", EPOCH + 91)
    book.stop(downstream, EPOCH + 120)
    with pytest.raises(CircuitBusy):          # 손상 미수리
        book.inject(source, "PM-SUCTION-LOSS", EPOCH + 400)
    assert book.repair(downstream, EPOCH + 400)[1] == [event.event_id]
    book.inject(source, "PM-SUCTION-LOSS", EPOCH + 461)


# 가정 기반 시나리오 (I5·I6): 물리 회로 밖이지만 주입할 수 있다
ASSUMED = {"C-2301": {"CC-SURGE"}, "B-1201": {"CC-SURGE"}, "C-1401": {"CC-SURGE", "CC-SURGE-HIGH-RATIO", "OP-LOW-FLOW"},
           "C-3101": {"CC-SURGE"}, "C-4101": {"CC-SURGE"}, "GB-3101": {"GB-LUBE-WEAR"},
           "GB-2301": {"GB-LUBE-WEAR"}, "GB-4101": {"GB-LUBE-WEAR"}}


def test_catalog_marks_supported_and_unsupported_targets(circuit):
    key, plant, _, physics, book = circuit
    catalog = build_catalog(plant, book, physics, EPOCH)
    assert (catalog["circuit"]["source"], catalog["circuit"]["downstream"]) == CIRCUITS[key][:2]
    by_id = {a["equip_id"]: a for a in catalog["equipment"]}
    assert set(by_id) == {e.equip_id for e in plant.equipment}
    for equip_id, asset in by_id.items():
        supported = {s["mode"] for s in asset["scenarios"] if s["supported"]}
        if equip_id == CIRCUITS[key][0]:
            assert supported == SOURCE_MODES | (CDU_SOURCE_EXTRA if key == "cdu" else set()) and asset["hydraulic_role"] == "source"
        elif equip_id == CIRCUITS[key][1]:
            assert supported == DOWNSTREAM_MODES | EXTRA_DOWNSTREAM.get(key, set())
            assert asset["hydraulic_role"] == "downstream"
        elif equip_id == CIRCUITS[key][2]:
            assert supported == DOWNSTREAM_MODES and asset["hydraulic_role"] == "source"
        else:
            assert supported == ASSUMED.get(equip_id, set()) and asset["hydraulic_role"] is None
            assert bool(asset["reason"]) == (not supported)
            for s in asset["scenarios"]:
                assert (s["model"], s["injectable"], s["reason"]) == (
                    ("empirical", True, None) if s["supported"] else ("empirical", False, OUTSIDE))
                assert not s["supported"] or not s["basis"]["physical_tags"]
        for scenario in asset["scenarios"]:
            assert set(scenario) == {"mode", "name", "model", "supported", "injectable", "blocked_by", "reason", "damage", "basis"}
    assert "label" not in str(catalog) and "expect" not in str(catalog)
    source = CIRCUITS[key][0]
    assert injectable(catalog, source, "PM-SUCTION-LOSS") == (200, None)
    book.inject(source, "PM-SUCTION-LOSS", EPOCH + 3)
    busy = build_catalog(plant, book, physics, EPOCH + 5)
    assert injectable(busy, CIRCUITS[key][1], "PM-IMPELLER-WEAR")[0] == 409
    asset = next(a for a in busy["equipment"] if a["equip_id"] == source)
    assert asset["state"] == "active" and asset["active_mode"] == "PM-SUCTION-LOSS" and asset["can_stop"]


def test_control_api_rejects_unsupported_and_noop_commands(config_dir, tmp_path):
    key = "hou"
    path = tmp_path / "runtime.yaml"
    runtime = load_runtime(config_dir / f"runtime_{key}.yaml").model_copy(update={
        "physics_general": False, "physics": config_dir / f"physics_{key}_integrated.yaml"})
    path.write_text(runtime.model_dump_json(), encoding="utf-8")
    app = create_single_app(config_dir / f"plant_{key}.yaml", path, ":memory:",
                            settings=Settings(with_simulators=False, ingest_token="local-test"))
    headers = {"X-Ingest-Token": "local-test"}
    with TestClient(app) as client:
        ctx = app.state.plants["plant"]
        ctx.simulation_clock = ControlledClock(START)
        assert client.get("/plant/simulation/catalog").status_code in (401, 403)
        catalog = client.get("/plant/simulation/catalog", headers=headers).json()
        assert catalog["plant_id"] == "HOU" and catalog["circuit"]["source"] == "P-2101"

        def control(n, **body):
            return client.post("/plant/simulation/control", headers=headers, json={"command_id": f"c{n}", **body})

        assert control(1, action="start").status_code == 200
        # 회로 밖 설비·검증하지 않은 경험 신호 모드는 주입하지 않는다.
        outside = control(2, action="inject", equip_id="C-2201", mode="CC-SURGE")
        assert outside.status_code == 422, outside.text
        assert control(3, action="inject", equip_id="P-2701", mode="PM-DISCHARGE-VALVE-RESTRICTION").status_code == 422
        # 멈추거나 고칠 사건이 없으면 성공으로 돌려주지 않는다.
        assert control(4, action="stop", equip_id="P-2101").status_code == 409
        assert control(5, action="repair", equip_id="P-2101").status_code == 409
        assert control(6, action="inject", equip_id="P-2101", mode="PM-SUCTION-LOSS").status_code == 200
        assert control(7, action="inject", equip_id="P-2701", mode="PM-IMPELLER-WEAR").status_code == 409
        assert control(8, action="stop", equip_id="P-2101").status_code == 200
        state = client.get("/plant/simulation/catalog", headers=headers).json()
        p2101 = next(a for a in state["equipment"] if a["equip_id"] == "P-2101")
        assert p2101["state"] in ("recovering", "active")


def test_control_api_injects_assumption_based_scenarios(config_dir, tmp_path):
    """가정 기반: GB-3101(I6)을 주입·정지·수리한다. 손상이 남은 동안은 다시 주입하지 않고, 물리 회로 주입은 막지 않는다."""
    app = create_single_app(config_dir / "plant_rhds.yaml", config_dir / "runtime_rhds_twin.yaml", ":memory:",
                            settings=Settings(with_simulators=False, ingest_token="local-test"))
    headers = {"X-Ingest-Token": "local-test"}
    with TestClient(app) as client:
        ctx = app.state.plants["plant"]
        ctx.simulation_clock = ControlledClock(START)

        def control(n, **body):
            return client.post("/plant/simulation/control", headers=headers, json={"command_id": f"c{n}", **body}).status_code

        def asset():
            rows = client.get("/plant/simulation/catalog", headers=headers).json()["equipment"]
            return next(a for a in rows if a["equip_id"] == "GB-3101")

        gear = {"equip_id": "GB-3101"}
        assert control(1, action="start") == 200
        assert control(2, action="inject", mode="GB-LUBE-WEAR", scenario_speed=10, **gear) == 200
        assert control(3, action="inject", mode="GB-LUBE-WEAR", **gear) == 409
        assert control(4, action="inject", equip_id="P-3101", mode="PM-SUCTION-LOSS") == 200
        assert control(5, action="inject", equip_id="F-3101", mode="FN-FOULING-SPEED-UP") == 422      # 가정 기반으로 열지 않은 모드
        event = next(e for e in ctx.book._events.values() if e.equip_id == "GB-3101")
        ctx.book.stop("GB-3101", event.start + 400)          # 배속 10: 치면 마모(S2) 뒤에 멈춤
        assert asset()["state"] == "damaged" and asset()["can_repair"]
        assert control(6, action="inject", mode="GB-LUBE-WEAR", **gear) == 409
        assert control(7, action="repair", **gear) == 200


def test_plant_without_physics_has_no_twin_control(config_dir, tmp_path):
    app = create_single_app(config_dir / "plant_rfcc.yaml", config_dir / "runtime_rfcc.yaml", ":memory:",
                            settings=Settings(with_simulators=False, ingest_token="local-test"))
    with TestClient(app) as client:
        assert client.get("/plant/simulation").status_code == 404
        assert client.get("/plant/simulation/catalog", headers={"X-Ingest-Token": "local-test"}).status_code == 404
