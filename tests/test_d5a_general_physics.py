"""PU-D5a: 일반 모드의 물리 회로 (physics_general). 중복 전파가 없고 회로 밖 경험 효과는 그대로다."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from collector.app import _require_physics_operator_token
from common.config_models import ConfigError, load_plant, load_runtime
from common.nelson import NelsonState
from scripts.calibrate_noise import fit
from simulator.fault import Effect, FaultEvent, load_fault_modes, plan
from simulator.interactions import _hold
from simulator.model import PlantGenerator
from simulator.physics_noise import Disturbance, loadings

ROOT = Path(__file__).resolve().parent.parent
KEYS = ("cdu", "hou", "rfcc", "rhds", "vrds")
MODES = load_fault_modes(ROOT / "config/fault_modes.yaml")
START = datetime(2026, 11, 12, 9, tzinfo=timezone(timedelta(hours=9)))
PAIRS = {"cdu": ("P-8101A", "P-8101B"), "rfcc": ("P-1101A", "P-1101B"), "hou": ("P-2101-S2", "P-2101"),
         "rhds": ("P-3101-S2", "P-3101"), "vrds": ("P-4101-S2", "P-4101")}     # (고장 펌프, 같이 변해야 하는 펌프)


@dataclass
class Events:
    items: list

    def external(self, start, end):
        return [e for e in self.items if e.start < end]


def setup(key):
    plant = load_plant(ROOT / f"config/plant_{key}.yaml")
    runtime = load_runtime(ROOT / f"config/runtime_{key}.yaml").model_copy(update={"quiet_normal": False})
    return plant, runtime


def mean_z(plant, runtime, events, at=2400, seconds=60):
    g = PlantGenerator(plant, runtime, Events(events))
    sums = {}
    for s in g.generate(START + timedelta(seconds=at), START + timedelta(seconds=at + seconds)):
        sums.setdefault((s.equip_id, s.tag_id), []).append(s.value)
    return {k: sum(v) / len(v) for k, v in sums.items()}


@pytest.mark.parametrize("key", KEYS)
def test_general_runtime_uses_the_twin_circuits_but_is_not_a_twin(key):
    runtime = load_runtime(ROOT / f"config/runtime_{key}.yaml")
    twin = load_runtime(ROOT / f"config/runtime_{key}_twin.yaml")
    assert runtime.physics == twin.physics and not runtime.twin and twin.twin
    # 일반 모드의 주입 목록은 예전처럼 토큰 없이 읽는다
    assert _require_physics_operator_token(None, SimpleNamespace(runtime=runtime)) is None


@pytest.mark.parametrize("key", KEYS)
def test_no_tag_gets_both_the_circuit_solution_and_an_empirical_effect(key):
    plant, runtime = setup(key)
    physics = PlantGenerator(plant, runtime).physics
    control = plant.control_by_key()
    for e in plant.equipment:
        for mode in MODES.values():
            if not mode.applies_to(e):
                continue
            tags, full = physics.tag_plan(mode, e.equip_id), plan(plant, mode, e.equip_id, control)
            if not physics.physical_parameters(mode, e.equip_id):
                assert tags == full                                     # 회로가 계산하지 않는 사건은 예전 그대로
                continue
            own = physics._owner[e.equip_id].tag_keys
            assert {k for k, tp in tags.items() if tp.via == "physics"} == own
            assert {k: tp for k, tp in tags.items() if k not in own} == {k: tp for k, tp in full.items() if k not in own}


@pytest.mark.parametrize("key", KEYS)
def test_fault_on_one_pump_moves_the_other_pump(key):
    plant, runtime = setup(key)
    faulty, other = PAIRS[key]
    event = FaultEvent("E", faulty, MODES["PM-SUCTION-LOSS"], START.timestamp())
    normal, fault = mean_z(plant, runtime, []), mean_z(plant, runtime, [event])
    sigma = {k: s for k, (_, s) in plant.control_by_key().items()}
    flow = next((other, t.tag_id) for t in plant.equipment_by_id()[other].tags if t.signal == "flow")
    change = (fault[flow] - normal[flow]) / sigma[flow]
    assert change > 2.5 if key in ("cdu", "rfcc") else change < -2.5    # 병렬은 부하를 받고 직렬은 유량이 같이 준다


def test_empirical_propagation_outside_the_circuit_is_kept():
    plant, runtime = setup("hou")
    event = FaultEvent("E", "P-2101", MODES["PM-SUCTION-LOSS"], START.timestamp())
    off = runtime.model_copy(update={"physics": None, "physics_general": False})
    a, b = mean_z(plant, runtime, [event]), mean_z(plant, off, [event])
    keys = [(("P-2601"), t.tag_id) for t in plant.equipment_by_id()["P-2601"].tags]
    assert {k: a[k] for k in keys} == {k: b[k] for k in keys}


@pytest.mark.parametrize("key", KEYS)
def test_linked_noise_keeps_sigma_and_follows_the_circuit(key):
    """원인 변동을 나눠 갖는 두 펌프: 정상 평균·σ 는 마스터 그대로, 유량은 함께 움직이고 직렬의 중간 압력도 함께 움직인다.
    전류(계산 태그)는 입력 태그의 흔들림을 식대로 따른다. 나눠 만들어도 값이 같다."""
    plant = load_plant(ROOT / f"config/plant_{key}.yaml")
    ids = plant.equipment_by_id()
    runtime = load_runtime(ROOT / f"config/runtime_{key}.yaml")
    first, second = sorted(PAIRS[key], key=len) if key in ("hou", "rhds", "vrds") else PAIRS[key]
    runtime = runtime.model_copy(update={"units": [ids[first].unit_id]})
    end = START + timedelta(hours=1)
    whole = PlantGenerator(plant, runtime).generate(START, end)
    g, parts = PlantGenerator(plant, runtime), []
    for m in range(0, 60, 20):
        parts += g.generate(START + timedelta(minutes=m), START + timedelta(minutes=m + 20))
    assert parts == whole
    v = {}
    for s in whole:
        k = (s.equip_id, s.tag_id)
        v.setdefault(k, []).append(s.value)
    control = plant.control_by_key()

    def tag(eid, signal):
        return next((eid, t.tag_id) for t in ids[eid].tags if t.signal == signal)

    for eid in (first, second):
        for signal in ("flow", "suction_pressure", "discharge_pressure", "current"):
            k = tag(eid, signal)
            mean, sigma = control[k]
            z = (np.array(v[k]) - mean) / sigma
            assert abs(z.mean()) < 0.1 and 0.9 < z.std() < 1.05, k
            if signal != "current":         # 억제 표집: 계산 태그가 아닌 태그는 Nelson 위반 0
                st = NelsonState()
                assert not any(st.push(float(x)) for x in z), k
    corr = lambda a, b: np.corrcoef(v[a], v[b])[0, 1]
    assert corr(tag(first, "flow"), tag(second, "flow")) > 0.95
    assert corr(tag(first, "flow"), tag(first, "discharge_pressure")) < -0.4        # 수요가 늘면 토출압이 내려간다
    if key in ("hou", "rhds", "vrds"):
        assert corr(tag(first, "discharge_pressure"), tag(second, "suction_pressure")) > 0.95
    else:
        assert corr(tag(first, "discharge_pressure"), tag(second, "discharge_pressure")) > 0.95
    t = next(t for t in ids[first].tags if t.signal == "current")
    ref = [plant.tag_by_key()[first, i].control.mean for i in t.derive.inputs]
    calc = [t.derive.value(list(x), ref, t.control.mean) for x in zip(*(v[first, i] for i in t.derive.inputs))]
    assert abs(np.std(np.array(v[first, t.tag_id]) - calc) / t.derive.noise_sigma - 1) < 0.1


def test_disturbance_larger_than_sigma_is_rejected():
    plant, runtime = setup("hou")
    a = next(x for x in PlantGenerator(plant, runtime).physics.adapters if hasattr(x, "second"))
    with pytest.raises(ConfigError):
        loadings(a, Disturbance(suction=0.1))


@pytest.mark.parametrize("key", KEYS)
def test_calibrated_disturbance_is_the_configured_one(key):
    disturbance, sigma = fit(key)
    configured = yaml.safe_load((ROOT / f"config/physics_{key}_twin.yaml").read_text(encoding="utf-8"))["disturbance"]
    assert disturbance == pytest.approx({k: v for k, v in configured.items() if k != "shared_header"}, rel=1e-3)
    control = load_plant(ROOT / f"config/plant_{key}.yaml").control_by_key()
    assert sigma == pytest.approx({k: control[k][1] for k in sigma}, rel=1e-3)


@pytest.mark.parametrize("suffix", ["", "_twin"])
@pytest.mark.parametrize("key", KEYS)
def test_events_the_circuit_does_not_support_never_enter_the_circuit(key, suffix):
    """시나리오 매핑에 없는 사건은 경험 효과만 쓴다: 회로 입력(막힘·마모)으로도 들어가지 않아야 한 태그에 두 번 걸리지 않는다."""
    plant = load_plant(ROOT / f"config/plant_{key}.yaml")
    physics = PlantGenerator(plant, load_runtime(ROOT / f"config/runtime_{key}{suffix}.yaml")).physics
    normal = physics.sensor_centers(physics.state(0, physics.parameter_plans([])))
    for e in plant.equipment:
        for mode in MODES.values():
            if mode.applies_to(e) and not physics.physical_parameters(mode, e.equip_id):
                plans = physics.parameter_plans([FaultEvent("E", e.equip_id, mode, 0.0)])
                assert physics.sensor_centers(physics.state(3000, plans)) == normal, (e.equip_id, mode.id)


@pytest.mark.parametrize("key, equip", [("rhds", "P-3102"), ("vrds", "P-4102")])
def test_i3_flow_interaction_works_for_the_hot_suction_fault(key, equip):
    """I3(흡입압 저하 × 온도 상승 → 유량 감소): 교호항을 끄면 유량이 덜 준다. 물리를 끈 생성과 같은 크기다(중복 없음)."""
    plant, runtime = setup(key)
    flow = next((equip, t.tag_id) for t in plant.equipment_by_id()[equip].tags if t.signal == "flow")
    event = FaultEvent("E", equip, MODES["PM-SUCTION-HOT"], START.timestamp())

    def level(rt, off=frozenset()):
        g = PlantGenerator(plant, rt, Events([event]), off)
        return np.mean([s.value for s in g.generate(START + timedelta(seconds=1500), START + timedelta(seconds=1560))
                        if (s.equip_id, s.tag_id) == flow])
    on, off = level(runtime), level(runtime, frozenset({"I3"}))
    sigma = plant.control_by_key()[flow][1]
    assert on < off - 5 * sigma
    assert abs(on - level(runtime.model_copy(update={"physics": None, "physics_general": False}))) < 0.1 * sigma


def test_i3_flow_interaction_of_p8303a_comes_from_the_circuit():
    """P-8303A 는 회로가 온도 상승의 증기압·NPSH 를 계산한다: 막힘에 온도 상승이 겹치면 유량이 더 준다."""
    plant, runtime = setup("cdu")
    loss, hot = (mean_z(plant, runtime, [FaultEvent("E", "P-8303A", MODES[m], START.timestamp())], at=1500)["P-8303A", "FI-8303A"]
                 for m in ("PM-SUCTION-LOSS", "PM-SUCTION-HOT"))
    assert hot < loss - 5 * plant.control_by_key()["P-8303A", "FI-8303A"][1]


@pytest.mark.parametrize("key, pump", [("cdu", "P-8303A"), ("rhds", "P-3102"), ("vrds", "P-4102")])
def test_i3_flow_interaction_is_added_on_top_of_the_circuit(key, pump):
    """흡입 막힘(회로)에 별도의 온도 상승이 겹치면 유량이 더 준다. 하나만이면 교호항을 꺼도 같다(원래 효과는 회로가 낸다)."""
    plant, runtime = setup(key)
    control, t0 = plant.control_by_key(), START.timestamp()
    flow = next((pump, t.tag_id) for t in plant.equipment_by_id()[pump].tags if t.signal == "flow")
    loss = FaultEvent("E1", pump, MODES["PM-SUCTION-LOSS"], t0)
    warm = _hold(pump, "warm", [Effect(signal="process_temp", shape="step", delta=15)], t0)

    def level(events, off=frozenset()):
        g = PlantGenerator(plant, runtime, Events(events), off)
        return np.mean([s.value for s in g.generate(START + timedelta(seconds=1500), START + timedelta(seconds=1560))
                        if (s.equip_id, s.tag_id) == flow])
    off = frozenset({"I3"})
    assert level([loss]) == level([loss], off)
    assert level([warm]) == level([warm], off)
    assert level([loss, warm]) < level([loss, warm], off) - 5 * control[flow][1]
    key_plan = PlantGenerator(plant, runtime).physics.tag_plan(MODES["PM-SUCTION-LOSS"], pump)[flow]
    assert key_plan.via == "physics" and key_plan.boost.by_tags()      # 정답지에는 물리 + 교호작용 요인으로 남는다
    if key == "cdu":    # 온도 상승을 회로가 계산하는 사건에는 더하지 않는다
        assert PlantGenerator(plant, runtime).physics.tag_plan(MODES["PM-SUCTION-HOT"], pump)[flow].extra is None


def test_cdu_normal_has_no_nelson_violation_against_mu():
    """정상 회로 해의 보정 잔차(CDU 최대 0.002σ) 때문에 억제 표집이 μ 기준 판정과 어긋나지 않는다."""
    start = START.replace(hour=0)      # 06:18:47 에 잔차 때문에 위반이 나던 날
    generator = PlantGenerator(load_plant(ROOT / "config/plant_cdu.yaml"), load_runtime(ROOT / "config/runtime_cdu.yaml"))
    tags = {t.key: t for t in generator._linked.tags}
    states = {k: NelsonState() for k in tags}
    bad = []
    for h in range(7):
        for s in generator.generate(start + timedelta(hours=h), start + timedelta(hours=h + 1)):
            k = (s.equip_id, s.tag_id)
            if k in tags and states[k].push((s.value - tags[k].mu) / tags[k].sigma):
                bad.append((k, s.ts))
    assert not bad
