"""교호작용 I1~I6: 대상, 2×2 × on/off 같은 시드 대조, 물리 태그 중복 금지, 손상 유지 (simulator.interactions)."""
from datetime import timedelta

import numpy as np
import pytest

from common.config_models import load_plant, load_runtime
from simulator.fault import Boost, FaultEvent, event_tag_effect, load_fault_modes, plan
from simulator.interactions import RULES, START, coverage, experiment, normal_low_flow, run_cell, targets
from simulator.model import PlantGenerator, _effect

PLANTS = ("cdu", "hou", "rfcc", "rhds", "vrds")


@pytest.fixture(scope="module")
def modes(config_dir):
    return load_fault_modes(config_dir / "fault_modes.yaml")


@pytest.fixture(scope="module")
def plants(config_dir):
    return {k: load_plant(config_dir / f"plant_{k}.yaml") for k in PLANTS}


class Fixed:
    def __init__(self, *events):
        self.events = list(events)

    def external(self, start, end):
        return self.events


def test_targets_match_the_reviewed_counts(plants, modes):
    """온톨로지 interaction_target 36건 + PU-D5a 새 펌프 5대의 I1 + 새 기어박스 2대의 I6 (온톨로지 반영은 PU-D5b)."""
    found = {r: [e for p in plants.values() for e in targets(r, p, modes)] for r in RULES}
    assert {r: len(v) for r, v in found.items()} == {"I1": 21, "I2": 7, "I3": 3, "I4": 4, "I5": 5, "I6": 3}
    assert found["I3"] == ["P-8303A", "P-3102", "P-4102"] and found["I6"] == ["GB-2301", "GB-3101", "GB-4101"] and "C-1401" in found["I5"]


@pytest.mark.parametrize("rule, key, equip", [
    ("I1", "rfcc", "P-1101A"), ("I2", "rfcc", "C-1401"), ("I3", "rhds", "P-3102"),
    ("I4", "rhds", "F-3101"), ("I5", "rfcc", "C-1401"), ("I6", "rhds", "GB-3101")])
def test_2x2_contrast_is_only_in_the_on_run(rule, key, equip, plants, modes, config_dir):
    rt = load_runtime(config_dir / f"runtime_{key}.yaml")
    rows = experiment(rule, plants[key], rt, equip, modes, seed=7)
    assert rows
    for r in rows:
        assert abs(r["contrast_off"]) < 0.01 * r["sigma"]           # 표본 반올림만 남는다
        want = -1 if (rule, r["signal"]) == ("I3", "flow") else 1
        assert r["contrast_on"] * want > 0.3 * r["sigma"]


def test_same_seed_is_reproducible_and_seeds_differ(plants, modes, config_dir):
    rt = load_runtime(config_dir / "runtime_rhds.yaml")
    a, b, c = (run_cell("I6", plants["rhds"], rt, "GB-3101", modes, (True, True), True, s) for s in (1, 1, 2))
    assert all(np.array_equal(a[t], b[t]) for t in a) and not np.array_equal(a["VI-O-GB3101"], c["VI-O-GB3101"])


def test_i1_on_is_the_configured_formula_and_off_is_additive(plants, config_dir):
    """on 은 구성안의 곱셈식, off 는 기준점 고정 가산식. 기준 운전점에서는 같다."""
    from simulator.model import additive_value
    t = plants["rfcc"].tag_by_key()[("P-1101A", "II-P-1101A")]
    ref = [plants["rfcc"].tag_by_key()[("P-1101A", i)].control.mean for i in t.derive.inputs]
    assert additive_value(t.derive, ref, ref, ref, t.control.mean) == pytest.approx(t.control.mean)
    only_flow = [ref[0] * 1.05, *ref[1:]]
    assert additive_value(t.derive, only_flow, ref, ref, t.control.mean) == pytest.approx(t.derive.value(only_flow, ref, t.control.mean))
    both = [ref[0] * 1.05, ref[1] * 1.05, ref[2]]
    assert additive_value(t.derive, both, ref, ref, t.control.mean) < t.derive.value(both, ref, t.control.mean)


def test_i5_normal_low_flow_has_no_pulsation(plants, modes, config_dir):
    rt = load_runtime(config_dir / "runtime_rfcc.yaml")
    normal = normal_low_flow(plants["rfcc"], rt, "C-1401", modes, 3)
    surge = run_cell("I5", plants["rfcc"], rt, "C-1401", modes, (True, False), True, 3)
    assert normal["FI-C-1401"].mean() == pytest.approx(surge["FI-C-1401"].mean(), rel=1e-4)      # 같은 저유량
    assert surge["FI-C-1401"].std() > 5 * normal["FI-C-1401"].std()
    assert surge["PD-C-1401"].std() > 3 * normal["PD-C-1401"].std()
    assert surge["VX-C-1401"].mean() > normal["VX-C-1401"].mean() + 5


def test_live_modes_execute_the_interactions(plants, modes):
    """주입할 수 있는 모드 자체에서 배율이 1 을 넘는다 (실험용 사건 없이)."""
    for key, equip, mode, tag, gain in [("rfcc", "C-1401", "CC-SURGE-HIGH-RATIO", "VX-C-1401", 1.5),
                                        ("rhds", "GB-3101", "GB-LUBE-WEAR", "VI-O-GB3101", 1.5),
                                        ("rhds", "F-3101", "FN-FOULING-SPEED-UP", "VI-A-F3101", 1.096),
                                        ("rhds", "P-3102", "PM-SUCTION-HOT", "VI-A-P3102", 1.45)]:
        tags = plan(plants[key], modes[mode], equip, plants[key].control_by_key())
        ev = FaultEvent("E", equip, modes[mode], 0.0)
        everything = {k: [(ev, tp)] for k, tp in tags.items()}
        base = event_tag_effect(tags[(equip, tag)], ev, 2600)[0]
        assert _effect(everything[(equip, tag)], 2600, everything)[0] == pytest.approx(base * gain, rel=0.01)
    tags = plan(plants["rfcc"], modes["CC-SURGE"], "C-1401", plants["rfcc"].control_by_key())
    ev = FaultEvent("E", "C-1401", modes["CC-SURGE"], 0.0)          # 압축비가 오르지 않으면 기존 크기 그대로
    everything = {k: [(ev, tp)] for k, tp in tags.items()}
    assert all(_effect(v, 601, everything)[0] == event_tag_effect(v[0][1], ev, 601)[0] for v in everything.values())


def test_pulsation_does_not_feed_back_into_its_gain(plants, modes):
    """요인 수준은 ramp·step 만 본다: 맥동 한 주기 동안 배율이 일정하다."""
    mode = modes["CC-SURGE-HIGH-RATIO"]
    tags = plan(plants["rfcc"], mode, "C-1401", plants["rfcc"].control_by_key())
    ev = FaultEvent("E", "C-1401", mode, 0.0)
    everything = {k: [(ev, tp)] for k, tp in tags.items()}
    tp = tags[("C-1401", "PD-C-1401")]
    for t in (1001, 1002, 1003, 1004):
        wobble = event_tag_effect(tp, ev, t, ("wobble",))[0]
        assert _effect([(ev, tp)], t, everything)[0] - event_tag_effect(tp, ev, t)[0] == pytest.approx(0.5 * wobble, abs=1e-9)


def test_i6_damage_keeps_its_gain_until_repair(plants, modes):
    mode = modes["GB-LUBE-WEAR"]
    tags = plan(plants["rhds"], mode, "GB-3101", plants["rhds"].control_by_key())
    key = ("GB-3101", "VI-O-GB3101")

    def z(t, stop=None, repair=None):
        ev = FaultEvent("E", "GB-3101", mode, 0.0, stop, repair=repair)
        return _effect([(ev, tags[key])], t, {k: [(ev, tp)] for k, tp in tags.items()})[0]
    held = z(2600, stop=2500)
    assert held == pytest.approx(z(2499)) == pytest.approx(15.0)      # 10σ × 1.5
    assert z(9000, stop=2500) == pytest.approx(held)                  # 압력·유온은 회복했지만 손상 크기는 그대로
    assert z(9000, stop=2500, repair=3000) == 0.0


@pytest.mark.parametrize("key", PLANTS)
def test_physical_tags_get_no_synthetic_effect(key, plants, modes, config_dir):
    """트윈: 어떤 모드를 걸어도 물리 회로 태그에는 합성 효과가 걸리지 않는다."""
    plant = plants[key]
    gen = PlantGenerator(plant, load_runtime(config_dir / f"runtime_{key}_twin.yaml"))
    assert gen.physics.tag_keys
    for e in plant.equipment:
        for m in modes.values():
            if m.applies_to(e):
                active = gen._active([FaultEvent(f"{e.equip_id}-{m.id}", e.equip_id, m, 0.0)])
                assert not set(active) & gen.physics.tag_keys, (e.equip_id, m.id)


def test_interaction_switch_leaves_physical_tags_alone(plants, modes, config_dir):
    plant, rt = plants["cdu"], load_runtime(config_dir / "runtime_cdu_twin.yaml")
    ev = FaultEvent("E", "P-8303A", modes["PM-SUCTION-HOT"], START.timestamp())
    runs = [PlantGenerator(plant, rt, Fixed(ev), off).generate(START + timedelta(seconds=1500), START + timedelta(seconds=1520))
            for off in (frozenset(), frozenset(RULES))]
    tags = PlantGenerator(plant, rt).physics.tag_keys
    on, off = ({(s.equip_id, s.tag_id, s.ts): s.value for s in r} for r in runs)
    assert {k: v for k, v in on.items() if k[:2] in tags} == {k: v for k, v in off.items() if k[:2] in tags}
    assert any(on[k] != off[k] for k in on if k[:2] == ("P-8303A", "VI-8303A"))        # 경험 신호는 스위치를 따른다


def test_coverage_marks_the_model_kind(plants, modes, config_dir):
    rows = coverage(plants["cdu"], load_runtime(config_dir / "runtime_cdu_twin.yaml"), modes)
    kind = {(r["rule"], r["tag_id"]): (r["model"], r["switchable"]) for r in rows}
    assert kind[("I1", "II-8101A")] == ("physics_coupled", False)
    assert kind[("I3", "FI-8303A")] == ("physics_coupled", False) and kind[("I3", "VI-8303A")] == ("empirical", True)
    rows = coverage(plants["rfcc"], load_runtime(config_dir / "runtime_rfcc.yaml"), modes)
    assert {r["model"] for r in rows if r["rule"] == "I5"} == {"synthetic_no_performance_map"}
    surge = "성능 맵 없음, 서지 한계·운전점을 계산하지 않는다"     # 물리 계산 없이 정한 효과의 근거 (같은 앵커를 셋이 나눠 쓴다)
    assert {m: modes[m].basis for m in ("CC-SURGE", "CC-SURGE-HIGH-RATIO", "OP-LOW-FLOW", "GB-LUBE-WEAR")} == {
        "CC-SURGE": surge, "CC-SURGE-HIGH-RATIO": surge, "OP-LOW-FLOW": surge, "GB-LUBE-WEAR": "치면 마모·기어 동역학을 계산하지 않는다"}


@pytest.mark.parametrize("change", [{"gain": None}, {"by": "speed"}, {"factors": []}, {"per_unit": 0.1}, {"hold_below_alarm": True}])
def test_factor_boost_is_checked(modes, change):
    row = modes["GB-LUBE-WEAR"].boost.model_dump() | change
    if change.get("factors") == []:
        row["shapes"] = []
    with pytest.raises(ValueError):
        Boost.model_validate(row)
