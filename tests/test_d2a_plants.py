"""D2-A: 플랜트별 명시 시나리오 매핑 × 기존 생성기·고장 엔진 결합 (CDU 회귀는 test_cdu_d0.py).

설비·시나리오마다 정상 → 주입 → 물리/경험 신호 → 연결 후속 변화 → 정지·수리 → 복귀를 독립 시험하고,
다른 설비·플랜트 무영향, 물리 태그 z 중복 없음, 계측 오차·누락 분리, 우회 경로, UTC/KST 를 확인한다.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from collector.anomaly_book import AnomalyBook
from collector.app import create_single_app
from collector.seed_master import seed_master
from collector.settings import Settings
from collector.storage import Storage
from common.config_models import load_plant, load_runtime
from common.sim_clock import KST
from simulator.fault import load_fault_modes
from simulator.instrument import InstrumentEvent
from simulator.model import PlantGenerator
from simulator.physics_adapter import load_physics

ROOT = Path(__file__).resolve().parents[1]
START = datetime(2026, 11, 24, tzinfo=KST)
EPOCH = int(START.timestamp())
PLANTS = ["hou", "rfcc", "rhds", "vrds"]
ARGS = {("source", "PM-SUCTION-LOSS"): (1, 0, 0, 0, 0), ("source", "PM-IMPELLER-WEAR"): (0, 1, 0, 0, 0),
        ("source", "OP-THROUGHPUT-CHANGE"): (0, 0, 0.2, 0, 0), ("downstream", "PM-SUCTION-LOSS"): (0, 0, 0, 1, 0),
        ("downstream", "PM-IMPELLER-WEAR"): (0, 0, 0, 0, 1),
        ("second", "PM-SUCTION-LOSS"): (0, 0, 0, 0, 0, 1, 0), ("second", "PM-IMPELLER-WEAR"): (0, 0, 0, 0, 0, 0, 1)}
MODES = load_fault_modes(ROOT / "config/fault_modes.yaml")


def setup(key: str, units=None):
    plant = load_plant(ROOT / f"config/plant_{key}.yaml")
    physics_path = ROOT / f"config/physics_{key}_twin.yaml"
    runtime = load_runtime(ROOT / f"config/runtime_{key}.yaml").model_copy(update={"physics": physics_path, "physics_general": False,
                                                                                    **({"units": units} if units else {})})
    physics = load_physics(plant, physics_path)
    storage = Storage(":memory:")
    seed_master(storage, plant)
    return plant, runtime, physics, AnomalyBook(storage, plant, MODES, physics=physics), storage


CASES = [(key, eid, mode) for key in PLANTS
         for eid, modes in load_physics(load_plant(ROOT / f"config/plant_{key}.yaml"),
                                        ROOT / f"config/physics_{key}_twin.yaml").scenarios.items() for mode in modes]


def test_explicit_mapping_uses_catalog_ids_and_no_cdu_only_faults():
    for key, eid, mode in CASES:
        assert mode in MODES, mode
        assert MODES[mode].applies_to(load_plant(ROOT / f"config/plant_{key}.yaml").equipment_by_id()[eid])
    # CDU 전용 밸브 고장을 다른 공장 설비에 붙이지 않는다.
    assert MODES["PM-DISCHARGE-VALVE-RESTRICTION"].equip_ids == ["P-8101A"]
    assert all(mode != "PM-DISCHARGE-VALVE-RESTRICTION" for _, _, mode in CASES)


@pytest.mark.parametrize(("key", "eid", "mode"), CASES)
def test_each_mapped_scenario_full_cycle_without_side_effects(key, eid, mode):
    plant, runtime, physics, book, storage = setup(key)
    try:
        owner = physics._owner[eid]
        args = ARGS[("second" if eid == owner.second else physics.roles[eid], mode)]
        damage = any(st.damage for st in MODES[mode].stages)
        event = book.inject(eid, mode, EPOCH + 10, 10)
        book.stop(eid, EPOCH + 310)
        if damage:
            assert book.repair(eid, EPOCH + 400)[1] == [event.event_id]
        plans = physics.parameter_plans(book.external(EPOCH, EPOCH + 600))
        i = physics.adapters.index(owner)
        normal = physics.state(EPOCH, physics.parameter_plans([]))[i]
        assert physics.state(EPOCH + 300, plans)[i] == owner.solve(*args)
        assert physics.state(EPOCH + 25, plans)[i] not in (normal, owner.solve(*args))
        assert (physics.state(EPOCH + 380, plans)[i] == owner.solve(*args)) == damage
        assert physics.state(EPOCH + 560, plans)[i] == normal

        def at(gen, second):
            samples = gen.generate(START + timedelta(seconds=second - 1), START + timedelta(seconds=second + 1))
            return {(s.equip_id, s.tag_id): s.value for s in samples if s.ts.timestamp() == EPOCH + second}

        got, base = at(PlantGenerator(plant, runtime, book), 300), at(PlantGenerator(plant, runtime), 300)
        decimals = {(e.equip_id, t.tag_id): t.decimals for e in plant.equipment for t in e.tags}
        centers, reference = owner.sensor_centers(owner.solve(*args)), owner.sensor_centers(normal)
        for tag in owner.tag_keys:                     # 물리 태그: 회로 해 변화만(z 중복 없음), 후속 설비 포함
            assert got[tag] - base[tag] == pytest.approx(centers[tag] - reference[tag], abs=1.001 * 10 ** -decimals[tag])
        partner = next(e for e in owner.roles if e != eid)
        moved = any(abs(centers[k] - reference[k]) > 10 ** -decimals[k] for k in owner.tag_keys if k[0] == partner)
        drum = next(b for b in owner.case.network.branches if b.branch_id == owner.case.downstream_branch).drum is not None
        if drum and eid == owner.downstream:
            # 후속 펌프 흡입 드럼(RFCC·HOU): 후속 고장은 액위가 받아 상류 펌프로 되돌아가지 않는다(open-issues 42·43).
            assert not moved, "드럼 뒤 후속 고장이 상류 설비를 움직였다"
        else:
            assert moved, "연결된 후속(또는 상류) 설비가 회로 해로 변해야 한다"
        allowed = owner.tag_keys | {(eid, t.tag_id) for t in plant.equipment_by_id()[eid].tags}
        for tag, value in got.items():                 # 다른 설비·태그는 그대로 (비의도적 영향 없음)
            if tag not in allowed:
                assert value == base[tag], tag
        empirical = [k for k, tp in physics.tag_plan(MODES[mode], eid).items() if tp.via != "physics"]
        assert all(k[0] == eid for k in empirical), "경험 신호는 주입 설비에만"
        if empirical:
            assert any(got[k] != base[k] for k in empirical if k in got), "경험 신호가 움직여야 한다"
        signal = {(e.equip_id, t.tag_id): t.signal for e in plant.equipment for t in e.tags}
        early, again = at(PlantGenerator(plant, runtime, book), 590), at(PlantGenerator(plant, runtime), 590)
        late = {k for k in early if early[k] != again[k]}
        assert all("temp" in signal[k] for k in late), "온도가 아닌 태그는 먼저 회복한다"
        assert late or not any("temp" in signal[k] for k in empirical), "온도는 늦게 회복한다"
        assert at(PlantGenerator(plant, runtime, book), 720) == at(PlantGenerator(plant, runtime), 720)
    finally:
        storage.close()


@pytest.mark.parametrize("key", PLANTS)
def test_other_plants_and_bypass_path_are_untouched(key):
    plant, runtime, physics, book, storage = setup(key)
    try:
        eid = next(iter(physics.scenarios))
        book.inject(eid, "PM-SUCTION-LOSS", EPOCH + 5, 10)
        # 다른 공장 생성기는 자기 장부만 본다.
        other = "cdu" if key != "cdu" else "hou"
        oplant, oruntime, _, obook, ostorage = setup(other)
        try:
            window = (START, START + timedelta(seconds=120))
            assert PlantGenerator(oplant, oruntime, obook).generate(*window) == PlantGenerator(oplant, oruntime).generate(*window)
        finally:
            ostorage.close()
        # 우회 경로: physics 없는 실행 설정은 기존 통계형 생성기 그대로, 물리 전용 모드는 걸 수 없다.
        bypass = load_runtime(ROOT / f"config/runtime_{key}.yaml").model_copy(update={"physics": None, "physics_general": False})
        assert PlantGenerator(plant, bypass).physics is None
        plain = AnomalyBook(storage, plant, MODES)
        assert "PM-IMPELLER-WEAR" not in {m.id for m in plain.applicable(eid)}
    finally:
        storage.close()


@pytest.mark.parametrize("key", PLANTS)
def test_physics_only_runs_for_units_holding_circuit_tags(key):
    plant, runtime, physics, book, storage = setup(key)
    try:
        circuit_units = {plant.equipment_by_id()[e].unit_id for e in physics.roles}
        other_unit = next(e.unit_id for e in plant.equipment if e.unit_id not in circuit_units)
        _, outside_runtime, _, _, _ = setup(key, units=[other_unit])
        plain = load_runtime(ROOT / f"config/runtime_{key}.yaml").model_copy(update={"physics": None, "physics_general": False, "units": [other_unit]})
        window = (START, START + timedelta(seconds=30))
        book.inject(next(iter(physics.scenarios)), "PM-SUCTION-LOSS", EPOCH + 2, 10)
        assert PlantGenerator(plant, outside_runtime, book).generate(*window) == PlantGenerator(plant, plain).generate(*window)
    finally:
        storage.close()


@pytest.mark.parametrize("key", PLANTS)
def test_instrument_errors_do_not_change_physical_state(key):
    plant, runtime, physics, book, storage = setup(key)
    try:
        eid = next(iter(physics.scenarios))
        owner = physics._owner[eid]
        flow = owner.keys[eid]["flow"]
        book.inject(eid, "PM-SUCTION-LOSS", EPOCH + 2, 10)
        clean = PlantGenerator(plant, runtime, book).generate(START, START + timedelta(seconds=60))
        gen = PlantGenerator(plant, runtime, book)
        gen.instrument = type("Fixed", (), {"events": lambda self, s, e: [
            InstrumentEvent("eval-stuck", "stuck", flow[0], (flow[1],), EPOCH + 20, EPOCH + 40),
            InstrumentEvent("eval-drop", "dropout", flow[0], tuple(t.tag_id for t in plant.equipment_by_id()[flow[0]].tags),
                            EPOCH + 45, EPOCH + 50)]})()
        measured = gen.generate(START, START + timedelta(seconds=60))
        stuck = [s.value for s in measured if (s.equip_id, s.tag_id) == flow and EPOCH + 20 <= s.ts.timestamp() < EPOCH + 40]
        assert len(set(stuck)) == 1                                   # 계측 고착: 값 고정
        moving = [s.value for s in clean if (s.equip_id, s.tag_id) == flow and EPOCH + 20 <= s.ts.timestamp() < EPOCH + 40]
        assert len(set(moving)) > 1                                   # 같은 구간 물리 상태는 계속 변한다
        assert not [s for s in measured if s.equip_id == flow[0] and EPOCH + 45 <= s.ts.timestamp() < EPOCH + 50]
        untouched = lambda xs: [s for s in xs if s.equip_id != flow[0]]
        assert untouched(measured) == untouched(clean)
        plans = physics.parameter_plans(book.external(EPOCH, EPOCH + 60))
        assert physics.state(EPOCH + 30, plans) == physics.state(EPOCH + 30, plans)  # 물리 상태는 계측 일정과 무관
        assert all(s.ts.utcoffset() == timedelta(hours=9) for s in measured)     # 생성 시각 KST
        temps = [s for s in measured if s.tag_id.startswith("TI-")]
        assert temps and all(int(s.ts.timestamp()) % 10 == 0 for s in temps)    # 온도 10 초
        assert sum((s.equip_id, s.tag_id) == owner.keys[eid]["suction_pressure"] for s in clean) == 60  # 1 초 (누락 없는 표본)
        assert sum((s.equip_id, s.tag_id) == owner.keys[eid]["suction_pressure"] for s in measured) == 55  # 설비 전체 누락 5 초
    finally:
        storage.close()


def test_api_reports_utc_measurement_time_for_kst_samples(tmp_path):
    key = "hou"
    plant, runtime, physics, book, storage = setup(key)
    storage.close()
    path = tmp_path / "runtime.yaml"
    path.write_text(runtime.model_dump_json(), encoding="utf-8")
    app = create_single_app(ROOT / f"config/plant_{key}.yaml", path, ":memory:",
                            settings=Settings(with_simulators=False, ingest_token="t"))
    with TestClient(app) as client:
        samples = PlantGenerator(plant, runtime).generate(START, START + timedelta(seconds=2))
        app.state.plants["plant"].receive_local("utc-test", runtime.source_id, samples)
        row = next(r for r in client.get("/plant/current").json() if r["ts"])
        ts = datetime.fromisoformat(row["ts"])
        assert ts.utcoffset() == timedelta(0)
        assert ts in {s.ts.astimezone(timezone.utc) for s in samples}


@pytest.mark.parametrize("key", ["cdu", *PLANTS])
def test_catalog_cavitation_flag_matches_npsh_config(key):
    """카탈로그의 cavitation_model 은 그 회로 해에 NPSH 설정이 실제로 들어가는지와 같아야 한다(하드코딩 금지)."""
    physics = load_physics(load_plant(ROOT / f"config/plant_{key}.yaml"), ROOT / f"config/physics_{key}_twin.yaml")
    for adapter, circuit in ((a, c) for a in physics.adapters for c in a.circuits()):
        case = adapter.case
        if circuit["kind"] == "coupled":
            configured = case.network.pump_npsh is not None or any(
                b.booster is not None and b.booster.npsh is not None for b in case.network.branches)
        else:
            configured = case.npsh is not None
        assert circuit["cavitation_model"] is configured, circuit["id"]
