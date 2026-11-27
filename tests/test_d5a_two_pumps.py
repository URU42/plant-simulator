"""PU-D5a: 두 번째 입력 펌프 다섯 대의 기본 구성 반영."""
from datetime import datetime, timedelta

import numpy as np
import pytest

from collector.anomaly_book import AnomalyBook, CircuitBusy
from collector.seed_master import seed_master
from collector.storage import Storage
from common.config_models import ConfigError, load_plant, load_runtime, load_yaml
from common.sim_clock import KST
from scripts.calibrate_physics import build
from scripts.twin_expectations import breach
from scripts.verify_circuits import open_plant
from simulator.fault import load_fault_modes, plan
from simulator.model import PlantGenerator
from simulator.physics_adapter import CoupledCase, PhysicsAdapter

PAIRS = {"cdu": ("P-8101A", "P-8101B"), "hou": ("P-2101", "P-2101-S2"), "rfcc": ("P-1101A", "P-1101B"),
         "rhds": ("P-3101", "P-3101-S2"), "vrds": ("P-4101", "P-4101-S2")}
START = datetime(2026, 11, 12, 1, tzinfo=KST)
EPOCH = int(START.timestamp())


@pytest.mark.parametrize("key", PAIRS)
def test_new_pump_mirrors_the_existing_one(key, config_dir):
    plant = load_plant(config_dir / f"plant_{key}.yaml")
    old, new = (plant.equipment_by_id()[e] for e in PAIRS[key])
    assert "합성 가정" in new.name and (old.type, old.unit_id, old.driven_by) == (new.type, new.unit_id, new.driven_by)
    circuit = ("control_valve_position", "header_flow")   # 회로 제어밸브 개도·헤더 유량은 회로에 하나라 한 대에만 있다(2026-11-27)
    for a, b in zip([t for t in old.tags if t.signal not in circuit], new.tags, strict=True):
        assert (a.signal, a.unit, a.interval_sec) == (b.signal, b.unit, b.interval_sec)
        assert a.tag_id != b.tag_id and a.seed_key != b.seed_key and "합성 설비" in b.limit_basis


@pytest.mark.parametrize("key", PAIRS)
def test_general_mode_normal_is_quiet_and_noise_is_independent(key, config_dir):
    """일반 모드 정상에서 한계 이탈이 없고, 두 펌프의 진동(공통 요인 없음) 잡음은 서로 독립이다."""
    plant = load_plant(config_dir / f"plant_{key}.yaml")
    rt = load_runtime(config_dir / f"runtime_{key}.yaml")
    assert rt.physics is not None and rt.physics_general
    unit = plant.equipment_by_id()[PAIRS[key][0]].unit_id
    rt = rt.model_copy(update={"units": [unit]})
    limits = {k: t.limits.model_dump(exclude_none=True) for k, t in plant.tag_by_key().items() if t.limits}
    values: dict[tuple[str, str], list[float]] = {}
    for s in PlantGenerator(plant, rt).generate(START, START + timedelta(hours=1)):
        assert breach(s.value, limits.get((s.equip_id, s.tag_id), {})) is None
        values.setdefault((s.equip_id, s.tag_id), []).append(s.value)
    a, b = ([t.tag_id for t in plant.equipment_by_id()[e].tags if t.signal == "vibration"][0] for e in PAIRS[key])
    r = np.corrcoef(values[PAIRS[key][0], a], values[PAIRS[key][1], b])[0, 1]
    assert abs(r) < 0.06


@pytest.mark.parametrize("key", PAIRS)
def test_one_physical_event_covers_both_pumps_and_no_stop_fault(key, config_dir):
    plant, _, physics, a = open_plant(key)
    modes = load_fault_modes(config_dir / "fault_modes.yaml")
    storage = Storage(":memory:")
    try:
        seed_master(storage, plant)
        book = AnomalyBook(storage, plant, modes, physics=physics)
        first, second = PAIRS[key]
        assert {m.id for m in book.applicable(second) if physics.physical_parameters(m, second)} == {"PM-SUCTION-LOSS", "PM-IMPELLER-WEAR"}
        book.inject(second, "PM-IMPELLER-WEAR", EPOCH, 10)
        for other in (first, a.downstream):
            with pytest.raises(CircuitBusy):
                book.inject(other, "PM-SUCTION-LOSS", EPOCH + 5)
        tags = physics.tag_plan(modes["PM-IMPELLER-WEAR"], second)
        assert {k for k, tp in tags.items() if tp.via == "physics"} == a.tag_keys
        assert all(k[0] == second for k, tp in tags.items() if tp.via != "physics")      # 그래프 전파 없음
    finally:
        storage.close()


@pytest.mark.parametrize("key", ["hou", "rhds", "vrds"])
def test_series_general_mode_propagates_by_graph_only(key, config_dir):
    plant = load_plant(config_dir / f"plant_{key}.yaml")
    mode = load_fault_modes(config_dir / "fault_modes.yaml")["OP-THROUGHPUT-CHANGE"]
    first, second = PAIRS[key]
    tags = plan(plant, mode, first, plant.control_by_key())
    assert any(k[0] == second and tp.via == "process" for k, tp in tags.items())
    assert second in plant.downstream([first], 1)


def test_station_must_match_the_master(config_dir, tmp_path):
    import yaml
    data = load_yaml(config_dir / "physics_hou_integrated.yaml")
    plant = load_plant(config_dir / "plant_hou.yaml")

    def rejected(update):
        path = tmp_path / "x.yaml"
        path.write_text(yaml.safe_dump({**data, **update}), encoding="utf-8")
        with pytest.raises(ConfigError):
            PhysicsAdapter(plant, path)
    st = data["station"]
    rejected({"station": {**st, "topology": "parallel"}})                                  # 경로와 배치가 다르다
    rejected({"station": {**st, "units": st["units"][::-1]}})                              # 첫 펌프가 equip_id 가 아니다
    rejected({"station": {**st, "units": [st["units"][0], {**st["units"][1], "unit_id": "P-9999"}]}})


@pytest.mark.parametrize("key", ["hou", "rfcc"])
def test_integrated_yaml_is_the_calibration_output(key, config_dir):
    cfg, _ = build(key)
    assert CoupledCase.model_validate(cfg) == CoupledCase.model_validate(load_yaml(config_dir / f"physics_{key}_integrated.yaml"))


def test_existing_history_survives_and_switch_time_is_kept(config_dir, tmp_path):
    """옛 구성의 DB 를 새 구성으로 열면 이력은 남고 새 태그·한계가 더해지며 전환 시각이 한 번 적힌다."""
    new = load_plant(config_dir / "plant_rfcc.yaml")
    drop = new.model_copy(update={"plant": new.plant.model_copy(update={"config_version": 8}),
                                  "equipment": [e for e in new.equipment if e.equip_id != "P-1101B"]})
    path = tmp_path / "old.db"
    storage = Storage(path)
    seed_master(storage, drop)
    storage.execute("INSERT INTO ts_raw (equip_id, tag_id, ts, value, batch_id, received_at) VALUES ('P-1101A', 'FI-P-1101A', '2026-11-12T00:00:00+00:00', 209.4, 'b', '2026-11-12T00:00:00+00:00')")
    storage.close()
    for _ in range(2):
        storage = Storage(path)
        seed_master(storage, new)
        assert storage.count("ts_raw") == 1 and storage.count("equipment") == len(new.equipment)
        rows = storage.query_dicts("SELECT config_version, applied_at FROM plant_config_change ORDER BY config_version")
        assert [r["config_version"] for r in rows] == [8, 9]
        values = storage.recent_values(5)
        storage.close()
    # 옛 μ 로 쌓인 마지막 값에서 이어도 새 값은 새 μ 둘레에서 바로 시작한다
    gen = PlantGenerator(new, load_runtime(config_dir / "runtime_rfcc.yaml"))
    gen.restore(values)
    mean, sigma = new.control_by_key()[("P-1101A", "FI-P-1101A")]
    got = [s.value for s in gen.generate(START, START + timedelta(seconds=60)) if s.tag_id == "FI-P-1101A"]
    assert all(abs(v - mean) < 4 * sigma for v in got)
