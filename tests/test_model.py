"""정상 운전 생성기와 고장 모드 효과 합성 (simulator.model)."""
from collections import defaultdict
from datetime import datetime, timedelta

import numpy as np
import pytest

from common.config_models import Operating, load_plant, load_runtime
from common.nelson import NelsonState
from common.sim_clock import KST
from simulator.fault import MAX_RECOVERY_SEC, RECOVERY_SEC, FaultEvent, load_fault_modes
from simulator.model import PlantGenerator

T0 = datetime(2026, 11, 12, 0, 0, tzinfo=KST)


class Fixed:
    """정해진 사건을 [start, end) 와 겹칠 때만 돌려주는 소스 (계약의 최소 구현)."""
    def __init__(self, events):
        self.events = events
    def external(self, start, end):
        return [e for e in self.events if e.start < end and (e.stop is None or e.stop + MAX_RECOVERY_SEC > start)]


@pytest.fixture(scope="module")
def modes(config_dir):
    return load_fault_modes(config_dir / "fault_modes.yaml")


@pytest.fixture
def rhds(config_dir):
    return load_plant(config_dir / "plant_rhds.yaml"), load_runtime(config_dir / "runtime_rhds.yaml")


@pytest.fixture
def rfcc(config_dir):
    return load_plant(config_dir / "plant_rfcc.yaml"), load_runtime(config_dir / "runtime_rfcc.yaml")


def violations(plant, samples, derived=False):
    """(태그 → 위반 표본 시각 목록). 판정은 운전점 μ·σ 기준 규칙 1~3. 계산 태그는 위반 0 을 보장하지 않으므로 derived=True 일 때만 본다."""
    ctl, state, out = plant.control_by_key(), defaultdict(NelsonState), defaultdict(list)
    skip = set() if derived else {k for k, t in plant.tag_by_key().items() if t.derive is not None}
    for s in samples:
        k = (s.equip_id, s.tag_id)
        if k not in skip and state[k].push((s.value - ctl[k][0]) / ctl[k][1]):
            out[k].append(s.ts.timestamp())
    return out


def by_tag(samples):
    out = defaultdict(list)
    for s in samples:
        out[(s.equip_id, s.tag_id)].append(s.value)
    return out


def test_normal_has_no_rule_violation_and_is_on_grid(rhds):
    plant, rt = rhds
    s = PlantGenerator(plant, rt).generate(T0, T0 + timedelta(hours=2))
    assert violations(plant, s) == {}
    assert all("status" not in x.model_dump() for x in s)
    tags = plant.tag_by_key()
    assert all(round(x.value, tags[(x.equip_id, x.tag_id)].decimals) == x.value for x in s)


def test_common_factor_correlates_within_unit_group_only(rfcc):
    """RFCC 가스 압축·회수 그룹(UG-RFCC-04)의 유량끼리는 상관(≈share 0.5), 다른 그룹과는 상관이 없다."""
    plant, rt = rfcc
    ctl = plant.control_by_key()
    z = {k: (np.array(v) - ctl[k][0]) / ctl[k][1]
         for k, v in by_tag(PlantGenerator(plant, rt).generate(T0, T0 + timedelta(hours=1))).items()}
    same = np.corrcoef(z[("C-1401", "FI-C-1401")], z[("P-1402A", "FI-P-1402A")])[0, 1]
    other = np.corrcoef(z[("C-1401", "FI-C-1401")], z[("P-1101A", "FI-P-1101A")])[0, 1]
    opposite = np.corrcoef(z[("C-1401", "FI-C-1401")], z[("C-1401", "PS-C-1401")])[0, 1]   # 흡입 압력은 반대 부호
    assert 0.4 < same < 0.6 and abs(other) < 0.1 and -0.6 < opposite < -0.4
    assert all(abs(v.mean()) < 0.2 and 0.85 < v.std() < 1.1 for v in z.values())


def test_fault_moves_only_planned_tags_then_recovers(rhds, modes):
    """C-3101 에 서지를 걸면 C-3101 과 공정 흐름 하류(P-3102·P-3103) 태그만 바뀌고(다른 설비는 사건이 없을 때와 같은 값),
    멈춘 뒤에는 정상으로 돌아온다."""
    plant, rt = rhds
    t = T0.timestamp() + 60
    ev = FaultEvent("E1", "C-3101", modes["CC-SURGE"], t, t + 400)
    end = T0 + timedelta(minutes=20)
    quiet = by_tag(PlantGenerator(plant, rt).generate(T0, end))
    hit_samples = PlantGenerator(plant, rt, Fixed([ev])).generate(T0, end)
    hit = by_tag(hit_samples)
    changed = {k for k in quiet if quiet[k] != hit[k]}
    assert ("C-3101", "FI-C3101") in changed and {k[0] for k in changed} == {"C-3101", "P-3102", "P-3103"}
    late = [x for x in hit_samples if x.ts.timestamp() >= t + 400 + MAX_RECOVERY_SEC + 90]   # 하류 회복 + 규칙 창(8표본 × 10초) 뒤
    assert violations(plant, late) == {}


def test_injected_rules_show_up_only_on_fault_equipment(rhds, modes):
    """서지 S1 의 유량 ramp(z −10)는 규칙 위반을 일으킨다. 판정은 보낸 값 기준."""
    plant, rt = rhds
    t = T0.timestamp() + 60
    s = PlantGenerator(plant, rt, Fixed([FaultEvent("E1", "C-3101", modes["CC-SURGE"], t)])).generate(T0, T0 + timedelta(minutes=4))
    v = violations(plant, s)
    assert v[("C-3101", "FI-C3101")] and min(v[("C-3101", "FI-C3101")]) >= t
    assert all(k[0] == "C-3101" for k in v)


def test_nelson_quiet_stage_has_no_violation(rfcc, modes):
    """수분 유입 S0(nelson_quiet) 동안 터빈 태그는 평균이 움직여도 규칙 1~3 을 어기지 않고, S2 부터는 어긴다."""
    plant, rt = rfcc
    t = T0.timestamp() + 60
    s = PlantGenerator(plant, rt, Fixed([FaultEvent("E1", "T-1401", modes["ST-WATER-INDUCTION"], t)])).generate(
        T0, T0 + timedelta(minutes=20))
    v = violations(plant, s)
    assert not [ts for k, tss in v.items() for ts in tss if k[0] == "T-1401" and ts < t + 900]
    assert [ts for ts in v[("T-1401", "VX-T-1401")] if ts >= t + 900]
    mu, sd = plant.control_by_key()[("T-1401", "VX-T-1401")]
    s0 = [(x.value - mu) / sd for x in s if x.tag_id == "VX-T-1401" and t + 300 <= x.ts.timestamp() < t + 900]
    assert 0.5 < np.mean(s0) < 1.1                              # ramp z 0.8 이 그대로 보인다


def test_driver_fault_reaches_driven_speed(rfcc, modes):
    """수분 유입 S2 의 터빈 회전속도 spike 는 피구동 압축기 회전속도에도 같은 값으로 나온다."""
    plant, rt = rfcc
    t = T0.timestamp() + 60
    g = PlantGenerator(plant, rt, Fixed([FaultEvent("E1", "T-1401", modes["ST-WATER-INDUCTION"], t)]))
    s = by_tag(g.generate(T0, T0 + timedelta(minutes=17)))
    assert s[("C-1401", "SI-C-1401")] == s[("T-1401", "SI-T-1401")]


def test_restore_seam_has_no_violation(rhds):
    plant, rt = rhds
    first = PlantGenerator(plant, rt).generate(T0, T0 + timedelta(minutes=10))
    b = PlantGenerator(plant, rt)
    b.restore({k: v[-8:] for k, v in by_tag(first).items()})
    nxt = b.generate(T0 + timedelta(minutes=10), T0 + timedelta(minutes=12))
    assert violations(plant, first + nxt) == {}


def test_deterministic(rhds):
    plant, rt = rhds
    def run():
        return PlantGenerator(plant, rt).generate(T0, T0 + timedelta(minutes=3))
    assert run() == run()


def test_unit_split_equals_whole(rfcc):
    """Unit 을 나눠 돌려도 같은 값이다 (공통 요인은 시드·공정 그룹에서, 피구동 speed 는 구동기 난수열에서)."""
    plant, rt = rfcc
    end = T0 + timedelta(minutes=5)
    whole = by_tag(PlantGenerator(plant, rt).generate(T0, end))
    for unit in plant.unit_ids():
        part = by_tag(PlantGenerator(plant, rt.model_copy(update={"units": [unit]})).generate(T0, end))
        assert all(part[k] == whole[k] for k in part), unit


def test_small_windows_equal_whole(rhds, modes):
    plant, rt = rhds
    t = T0.timestamp() + 30
    ev = FaultEvent("E1", "C-3101", modes["CC-SURGE"], t, t + 200)
    whole = PlantGenerator(plant, rt, Fixed([ev])).generate(T0, T0 + timedelta(seconds=400))
    g, split = PlantGenerator(plant, rt, Fixed([ev])), []
    for k in range(40):
        split += g.generate(T0 + timedelta(seconds=10 * k), T0 + timedelta(seconds=10 * k + 10))
    assert split == whole


def test_rfcc_driven_speed_equal(rfcc):
    plant, rt = rfcc
    s = by_tag(PlantGenerator(plant, rt).generate(T0, T0 + timedelta(minutes=2)))
    assert s[("B-1201", "SI-B-1201")] == s[("E-1202", "SI-E-1202")] and s[("C-1401", "SI-C-1401")] == s[("T-1401", "SI-T-1401")]


@pytest.mark.parametrize("key", ["rhds", "vrds", "hou"])
def test_compressor_speed_follows_motor(config_dir, key):
    """직결 전동기는 압축기와 같은 값, M-3101 → GB-3101 → C-3101 연쇄는 기어비(9210.1/1790)만큼 반올림 오차 안에서 같다."""
    plant, rt = load_plant(config_dir / f"plant_{key}.yaml"), load_runtime(config_dir / f"runtime_{key}.yaml")
    s = PlantGenerator(plant, rt).generate(T0, T0 + timedelta(hours=1))
    def speed(eq):
        return [x.value for x in s if x.equip_id == eq and plant.tag_by_key()[(eq, x.tag_id)].signal == "speed"]
    if key != "rhds":
        direct = {"vrds": ("M-4102", "C-4102"), "hou": ("M-2201", "C-2201")}[key]
        motor, gear, comp, ratio = {"vrds": ("M-4101", "GB-4101", "C-4101", 9203.6 / 1790), "hou": ("M-2301", "GB-2301", "C-2301", 5285.1 / 1790)}[key]
        assert speed(direct[0]) == speed(direct[1]) and speed(gear) == speed(comp)
        assert max(abs(c - m * ratio) for m, c in zip(speed(motor), speed(comp))) <= 0.05 * ratio + 0.05 + 1e-9
        return
    assert speed("M-3102") == speed("C-3102") and speed("GB-3101") == speed("C-3101")
    assert max(abs(c - m * 9210.1 / 1790) for m, c in zip(speed("M-3101"), speed("C-3101"))) <= 0.05 + 1e-9
    assert violations(plant, s) == {}


def test_operating_point_shifts_generated_values(rfcc):
    """T-1401 운전점을 설계의 1.05 배로 두면 터빈·피구동 압축기 회전속도와 압축기 유량이 그 비율로 옮겨 가고, 거기서도 조용하다."""
    plant, rt = rfcc
    design = plant.tag_by_key()[("T-1401", "SI-T-1401")].control.mean
    flow = plant.tag_by_key()[("C-1401", "FI-C-1401")].control.mean
    moved = plant.model_copy(deep=True)
    moved.equipment_by_id()["T-1401"].operating = Operating(speed_rpm=design * 1.05)
    samples = PlantGenerator(moved, rt).generate(T0, T0 + timedelta(minutes=10))
    s = by_tag(samples)
    assert np.mean(s[("T-1401", "SI-T-1401")]) == pytest.approx(design * 1.05, rel=1e-3)
    assert s[("C-1401", "SI-C-1401")] == s[("T-1401", "SI-T-1401")]
    assert np.mean(s[("C-1401", "FI-C-1401")]) == pytest.approx(flow * 1.05, rel=2e-3)
    assert violations(moved, samples) == {}


@pytest.mark.parametrize("mode_id", ["ST-WATER-INDUCTION", "CC-SURGE", "PM-SUCTION-LOSS", "OP-LOAD-CHANGE", "FN-IMPELLER-FOULING",
                                     "EX-CATALYST-EROSION", "RC-VALVE-LEAK", "RC-LIQUID-INGRESS", "GB-LUBE-WEAR"])
def test_each_mode_changes_only_its_planned_tags(config_dir, modes, mode_id):
    """고장 모드마다 첫 대상 설비에 걸어 3분 생성: 바뀐 태그는 계획에 있는 태그(피구동 speed 포함)뿐이다."""
    from simulator.fault import plan
    for key in ("rfcc", "cdu", "hou", "rhds", "vrds"):
        plant = load_plant(config_dir / f"plant_{key}.yaml")
        target = next((e.equip_id for e in plant.equipment if modes[mode_id].applies_to(e)
                       and plan(plant, modes[mode_id], e.equip_id, plant.control_by_key())), None)
        if target:
            break
    rt = load_runtime(config_dir / f"runtime_{key}.yaml")
    ev = FaultEvent("E1", target, modes[mode_id], T0.timestamp())
    end = T0 + timedelta(minutes=3)
    base, hit = by_tag(PlantGenerator(plant, rt).generate(T0, end)), by_tag(PlantGenerator(plant, rt, Fixed([ev])).generate(T0, end))
    changed = {k for k in base if base[k] != hit[k]}
    planned = set(PlantGenerator(plant, rt).physics.tag_plan(modes[mode_id], target))      # 회로가 계산하는 사건이면 회로의 물리 태그 전부
    driven_speed = {k for k in base if plant.tag_by_key()[k].signal == "speed"}
    assert changed and changed <= planned | driven_speed, (mode_id, changed - planned)


@pytest.mark.parametrize("key, equip, tag", [("rfcc", "T-1401", "STT-T-1401"), ("rhds", "C-3102", "PI-I-C3102"),
                                             ("rfcc", "P-1101A", "II-P-1101A"), ("rhds", "C-3101", "TI-D-C3101"),
                                             ("cdu", "F-8201", "PDI-8201A")])
def test_derived_tag_follows_inputs_at_each_sample(config_dir, key, equip, tag):
    """계산 태그 − 그 시각 입력의 계산식 = 자체 잡음(σ ≈ noise_sigma). 생성값의 σ 는 구성안 σ 와 같고,
    정상 위반은 드물다(1 % 미만, 보장은 아님)."""
    plant, rt = load_plant(config_dir / f"plant_{key}.yaml"), load_runtime(config_dir / f"runtime_{key}.yaml")
    tags = plant.tag_by_key()
    d = tags[(equip, tag)].derive
    ref = ([tags[(equip, i)].control.mean for i in d.inputs], tags[(equip, tag)].control.mean)
    s = PlantGenerator(plant, rt).generate(T0, T0 + timedelta(hours=2))
    at = defaultdict(dict)
    for x in s:
        at[(x.equip_id, x.tag_id)][x.ts] = x.value
    resid = [v - d.value([at[(equip, i)][ts] for i in d.inputs], *ref) for ts, v in at[(equip, tag)].items()]
    assert abs(np.mean(resid)) < 0.2 * d.noise_sigma and np.std(resid) == pytest.approx(d.noise_sigma, rel=0.15)
    assert np.std(list(at[(equip, tag)].values())) == pytest.approx(plant.control_by_key()[(equip, tag)][1], rel=0.05)
    assert len(violations(plant, s, derived=True).get((equip, tag), [])) < 0.01 * len(resid)


def test_derived_tag_moves_with_input_fault(config_dir, modes):
    """액체 유입은 토출 압력만 흔드는데, 단간 압력(계산 태그)이 따라 흔들리고 정답지 계획에도 들어간다."""
    from simulator.fault import plan
    plant, rt = load_plant(config_dir / "plant_rhds.yaml"), load_runtime(config_dir / "runtime_rhds.yaml")
    mode = modes["RC-LIQUID-INGRESS"]
    tp = plan(plant, mode, "C-3102", plant.control_by_key())[("C-3102", "PI-I-C3102")]
    assert tp.applied and all(a.z == 0.0 for a in tp.applied)     # 자기 효과 없이 입력 효과의 시작만 물려받는다
    ev = FaultEvent("E1", "C-3102", mode, T0.timestamp())
    end = T0 + timedelta(minutes=10)
    base = by_tag(PlantGenerator(plant, rt).generate(T0, end))[("C-3102", "PI-I-C3102")]
    hit = by_tag(PlantGenerator(plant, rt, Fixed([ev])).generate(T0, end))[("C-3102", "PI-I-C3102")]
    assert np.std(np.subtract(hit, base)) > 0.05     # 정상 σ 0.043 보다 큰 흔들림


def test_unfiltered_normal_has_chance_violations(rhds):
    """quiet_normal: false 면 억제 표집을 하지 않는다. 규칙 1 비율은 정규분포의 ±3σ 밖(약 0.27 %) 근처여야 한다."""
    plant, rt = rhds
    raw = rt.model_copy(update={"quiet_normal": False})
    s = PlantGenerator(plant, raw).generate(T0, T0 + timedelta(hours=2))
    assert violations(plant, s)
    ctl, base = plant.control_by_key(), [x for x in s if plant.tag_by_key()[(x.equip_id, x.tag_id)].derive is None]
    outside = sum(abs(x.value - ctl[(x.equip_id, x.tag_id)][0]) > 3 * ctl[(x.equip_id, x.tag_id)][1] for x in base) / len(base)
    assert 0.001 < outside < 0.006
    assert PlantGenerator(plant, rt).generate(T0, T0 + timedelta(minutes=3)) != PlantGenerator(plant, raw).generate(T0, T0 + timedelta(minutes=3))
