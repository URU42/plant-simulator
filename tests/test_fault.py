"""고장 모드 정의와 효과 계산 (simulator.fault)."""
import math

import pytest

from common.config_models import ConfigError, load_plant
from simulator.fault import RECOVERY_SEC, Effect, FaultMode, load_fault_modes, plan, tag_effect


@pytest.fixture(scope="module")
def modes(config_dir):
    return load_fault_modes(config_dir / "fault_modes.yaml")


def _mode(stages, **kw):
    return FaultMode.model_validate({"id": "X", "name": "x", "equip_types": ["PM"],
                                     "label": {"kind": "k", "cause": "c", "action": "a"}, "stages": stages} | kw)


def test_repo_fault_modes_load_and_apply(modes, config_dir):
    """저장소의 고장 모드는 모두 읽히고, 각각 적어도 한 공장의 설비 하나에 효과가 걸린다."""
    plants = [load_plant(config_dir / f"plant_{k}.yaml") for k in ("rfcc", "cdu", "hou", "rhds", "vrds")]
    for m in modes.values():
        assert any(plan(p, m, e.equip_id, p.control_by_key()) for p in plants for e in p.equipment if m.applies_to(e)), m.id


@pytest.mark.parametrize("bad", [
    {"signal": "flow", "shape": "step"},                                   # 크기 없음
    {"signal": "flow", "shape": "wobble", "z": 1},                         # 주기 없음
    {"signal": "flow", "shape": "spike", "z": 1, "rise_sec": 5},           # 가라앉는 시간 없음
])
def test_effect_validation(bad):
    with pytest.raises(ValueError):
        Effect.model_validate(bad)


def test_stage_order_and_target_required():
    with pytest.raises(ValueError):
        _mode([{"id": "A", "name": "a", "at_sec": 10}, {"id": "B", "name": "b", "at_sec": 5}])
    with pytest.raises(ValueError):
        _mode([{"id": "A", "name": "a", "at_sec": 0}], equip_types=[])


def test_duplicate_mode_id(tmp_path):
    p = tmp_path / "m.yaml"
    one = "{id: X, name: x, equip_types: [PM], label: {kind: k, cause: c, action: a}, stages: [{id: A, name: a, at_sec: 0}]}"
    p.write_text(f"fault_modes: [{one}, {one}]\n")
    with pytest.raises(ConfigError):
        load_fault_modes(p)


def test_to_limit_reaches_limit_and_subtracts_earlier_level(config_dir):
    """to_limit 은 효과 합이 그 한계값(+margin)에 닿게 한다. 앞 단계 ramp 가 남긴 수준을 빼고 움직인다."""
    plant = load_plant(config_dir / "plant_cdu.yaml")
    control = plant.control_by_key()
    m = _mode([
        {"id": "S1", "name": "a", "at_sec": 0, "effects": [{"signal": "vibration", "shape": "ramp", "to_limit": "warn_hi", "rise_sec": 10}]},
        {"id": "S2", "name": "b", "at_sec": 20, "effects": [
            {"signal": "vibration", "shape": "ramp", "to_limit": "alarm_hi", "margin": 0.1, "rise_sec": 10},
            {"signal": "vibration", "shape": "spike", "z": 50, "rise_sec": 1, "decay_sec": 1}]},     # spike 는 수준을 남기지 않는다
        {"id": "S3", "name": "c", "at_sec": 40, "effects": [{"signal": "vibration", "shape": "ramp", "to_limit": "trip_hi", "rise_sec": 10}]},
    ])
    tp = plan(plant, m, "P-8101A", control)[("P-8101A", "VI-8101A")]
    mu, sd = control[("P-8101A", "VI-8101A")]
    def value(t):
        return mu + sd * tag_effect(tp, t, None)[0]
    assert value(15) == pytest.approx(3.0)                      # warn_hi
    assert value(35) == pytest.approx(mu + (4.5 - mu) * 1.1)    # alarm_hi + 10 %
    assert value(55) == pytest.approx(7.1)                      # trip_hi


def test_to_limit_falls_back_to_z_or_skips(config_dir):
    """합성 기어박스 GB-3101 진동 태그 2개에는 한계값이 없다: z 가 있으면 z, 없으면 그 태그에는 걸지 않는다."""
    plant = load_plant(config_dir / "plant_rhds.yaml")
    with_z = _mode([{"id": "S", "name": "s", "at_sec": 0,
                     "effects": [{"signal": "vibration", "shape": "step", "to_limit": "alarm_hi", "z": 7}]}])
    without = _mode([{"id": "S", "name": "s", "at_sec": 0,
                      "effects": [{"signal": "vibration", "shape": "step", "to_limit": "alarm_hi"}]}])
    tp = plan(plant, with_z, "GB-3101", plant.control_by_key())
    assert [a.z for t in tp.values() for a in t.applied] == [7, 7]
    assert plan(plant, without, "GB-3101", plant.control_by_key()) == {}


def test_propagation_follows_shaft_chain_and_skips_driven_speed(config_dir, modes):
    """M-3101 → GB-3101 → C-3101: 아래쪽 설비 모두에 걸고, 피구동 speed 는 구동기 값을 쓰므로 빼고,
    그 밖에는 공정 흐름 하류(P-3102·P-3103)에만 건다."""
    plant = load_plant(config_dir / "plant_rhds.yaml")
    tags = plan(plant, modes["OP-LOAD-CHANGE"], "M-3101", plant.control_by_key())
    keys = set(tags)
    assert ("M-3101", "SI-M3101") in keys and ("C-3101", "FI-C3101") in keys
    assert not {k for k in keys if k[0] in ("GB-3101", "C-3101") and "SI-" in k[1]}
    assert {k[0] for k in keys} <= {"M-3101", "GB-3101", "C-3101", "P-3102", "P-3103"}
    assert {(tp.via, tp.hop) for k, tp in tags.items() if k[0] == "C-3101"} == {("driven", 0)}
    assert {(tp.via, tp.hop) for k, tp in tags.items() if k[0] == "P-3102"} == {("process", 1)}


def test_shapes_and_recovery():
    m = _mode([{"id": "S", "name": "s", "at_sec": 10, "nelson_quiet": True, "effects": [
        {"signal": "flow", "shape": "ramp", "z": 4, "rise_sec": 20},
        {"signal": "flow", "shape": "wobble", "z": 1, "period_sec": 8, "lag_sec": 100}]}])
    tp = plan_one(m)
    assert tag_effect(tp, 5, None) == (0.0, False)              # 단계 전: 효과 없음, quiet 도 아님
    assert tag_effect(tp, 20, None) == (2.0, True)              # ramp 절반
    assert tag_effect(tp, 50, None)[0] == 4.0
    assert tag_effect(tp, 112, None)[0] == pytest.approx(5.0)   # + wobble 꼭대기 (시작 110 + 1/4 주기)
    stop = 60
    assert tag_effect(tp, 60 + RECOVERY_SEC / 2, stop) == (2.0, False)   # 멈춘 뒤 절반 회복, quiet 끝
    assert tag_effect(tp, 60 + RECOVERY_SEC, stop) == (0.0, False)
    assert tag_effect(tp, 112, stop)[0] == pytest.approx(4 * (1 - 52 / RECOVERY_SEC))   # 멈추기 전에 시작하지 못한 wobble 은 없다


def test_spike_returns_to_zero():
    tp = plan_one(_mode([{"id": "S", "name": "s", "at_sec": 0,
                          "effects": [{"signal": "flow", "shape": "spike", "z": -6, "rise_sec": 2, "decay_sec": 4}]}]))
    assert [tag_effect(tp, t, None)[0] for t in (0, 1, 2, 4, 6, 9)] == [0, -3, -6, -3, 0, 0]


def plan_one(mode):
    """가짜 구성 없이 CDU P-8101A 의 유량 태그 하나로 계획을 만든다."""
    from pathlib import Path
    plant = load_plant(Path(__file__).resolve().parent.parent / "config" / "plant_cdu.yaml")
    return plan(plant, mode, "P-8101A", plant.control_by_key())[("P-8101A", "FI-8101A")]


def test_wobble_is_zero_mean_over_period():
    tp = plan_one(_mode([{"id": "S", "name": "s", "at_sec": 0,
                          "effects": [{"signal": "flow", "shape": "wobble", "z": 2, "period_sec": 8}]}]))
    assert math.fsum(tag_effect(tp, t, None)[0] for t in range(8)) == pytest.approx(0, abs=1e-9)


def test_every_equipment_has_a_fault_mode(modes, config_dir):
    """다섯 공장 42대 모두 걸 수 있는 고장 모드가 하나 이상 있다."""
    missing = []
    for key in ("rfcc", "cdu", "hou", "rhds", "vrds"):
        plant = load_plant(config_dir / f"plant_{key}.yaml")
        control = plant.control_by_key()
        missing += [e.equip_id for e in plant.equipment
                    if not any(m.applies_to(e) and plan(plant, m, e.equip_id, control) for m in modes.values())]
    assert missing == []


def test_centrifugal_only_modes_skip_reciprocating(modes):
    """설비 유형 CC 는 원심·왕복동을 구분하지 않으므로 서지·왕복동 모드는 설비 ID 로 고른다."""
    assert not set(modes["CC-SURGE"].equip_ids) & set(modes["RC-VALVE-LEAK"].equip_ids)


def test_speed_compresses_stage_times_and_ramps_not_wobble():
    """진행 속도 ×10: 효과 시작(단계 + lag)과 ramp 상승은 1/10, wobble 주기·회복은 실제 시간 그대로."""
    m = _mode([{"id": "A", "name": "a", "at_sec": 100, "nelson_quiet": True,
                "effects": [{"signal": "flow", "shape": "ramp", "z": 4, "rise_sec": 600}]},
               {"id": "B", "name": "b", "at_sec": 1000, "effects": [{"signal": "flow", "shape": "wobble", "z": 1, "period_sec": 8}]}])
    from common.config_models import load_plant
    import pathlib
    plant = load_plant(pathlib.Path(__file__).resolve().parent.parent / "config" / "plant_rhds.yaml")
    tp = plan(plant, m, "P-3101", plant.control_by_key())[("P-3101", "FI-P3101")]
    assert tag_effect(tp, 9, None, 10) == (0.0, False)                  # 단계 A 는 100/10 = 10초부터
    assert tag_effect(tp, 40, None, 10) == (2.0, True)                  # ramp 600/10 = 60초의 절반
    assert tag_effect(tp, 70, None, 10)[0] == 4.0
    assert tag_effect(tp, 102, None, 10) == (pytest.approx(5.0), False)  # 단계 B 는 100초부터, 주기 8초 그대로(1/4 주기 = 2초)
    assert tag_effect(tp, 70, 70, 10)[0] == 4.0 and tag_effect(tp, 100, 70, 10)[0] == pytest.approx(2.0)   # 회복 60초 그대로


def test_speed_keeps_lag_real_time():
    """lag(물리적 응답 지연)는 진행 속도와 상관없이 실제 시간: 단계 100초·lag 22초 → ×10 이면 10 + 22 = 32초부터."""
    from simulator.fault import effect_start
    assert effect_start(122, 22, 10) == 32 and effect_start(122, 22, 1) == 122


def test_flow_reach_matches_viewer_rules(config_dir):
    """흐름 그래프 단계: 공정 노드를 지나 설비에 닿을 때마다 1단계, utility 로 닿은 설비에서 멈춘다(초안 v0.3 도달 표)."""
    rfcc = load_plant(config_dir / "plant_rfcc.yaml")
    r = rfcc.downstream(["P-1101A"], 2)
    assert {d for d, (k, _) in r.items() if k == 1} == {"C-1401", "P-1301A", "P-1302A", "P-1303A"}
    assert {d for d, (k, _) in r.items() if k == 2} == {"P-1402A", "P-1403A", "P-1501A", "P-1502A"}
    assert set(rfcc.downstream(["P-1101A"], 1)) == {"C-1401", "P-1301A", "P-1302A", "P-1303A"}
    hou = load_plant(config_dir / "plant_hou.yaml")
    assert hou.downstream(["P-2501"], 2) == {"C-2301": (1, "utility")}             # 윤활유: 압축기에서 멈춤
    assert hou.downstream(["C-2201"], 2)["C-2301"] == (1, "utility")                # 가상 공장 간선
    assert "C-3101" not in load_plant(config_dir / "plant_rhds.yaml").downstream(["P-3101"], 2)   # 공정 흐름 뒤 수소 계통으로 안 퍼짐
    cdu = load_plant(config_dir / "plant_cdu.yaml")
    assert cdu.downstream(["P-8303A"], 2) == {}


def test_downstream_effects_shrink_slow_and_recover_late(config_dir, modes):
    """k단계: 크기 ×0.7^(k−1), lag·rise ×k, 회복 60초 ×(k+1). 0.25σ 이하로 줄면 걸지 않는다."""
    plant = load_plant(config_dir / "plant_rfcc.yaml")
    tags = plan(plant, modes["PM-SUCTION-LOSS"], "P-1101A", plant.control_by_key())
    one, two = tags[("C-1401", "FI-C-1401")], tags[("P-1402A", "FI-P-1402A")]
    assert (one.via, one.hop, one.recovery) == ("process", 1, 2 * RECOVERY_SEC)
    assert (two.hop, two.recovery) == (2, 3 * RECOVERY_SEC)
    (a1,), (a2,) = one.applied, two.applied
    assert (a1.z, a1.start, a1.effect.rise_sec) == (-1.0, 300 + 60, 300)
    assert (a2.z, a2.start, a2.effect.rise_sec) == (pytest.approx(-0.7), 300 + 120, 600)
    stop = 1000.0
    assert tag_effect(one, stop + 2 * RECOVERY_SEC, stop)[0] == 0.0 and tag_effect(two, stop + 2 * RECOVERY_SEC, stop)[0] != 0.0
    from simulator.fault import _hop_effect
    assert _hop_effect(Effect(signal="flow", shape="ramp", z=0.3), 2) is None        # 0.21σ: 걸지 않는다


def test_downstream_effect_validation():
    with pytest.raises(ValueError):
        FaultMode.model_validate({"id": "X", "name": "x", "equip_types": ["PM"], "label": {"kind": "k", "cause": "c", "action": "a"},
                                  "stages": [{"id": "S1", "name": "s", "at_sec": 0, "propagate": {"process": [
                                      {"signal": "flow", "shape": "step", "z": -1}]}}]})


def test_damage_stage_holds_after_stop_until_repair():
    """손상 단계의 ramp 는 멈춘 시각의 크기로 남고, 수리 뒤 회복 시간 동안 0 이 된다. spike 는 남지 않는다."""
    tp = plan_one(_mode([
        {"id": "S1", "name": "a", "at_sec": 0, "effects": [{"signal": "flow", "shape": "ramp", "z": 2, "rise_sec": 10}]},
        {"id": "S2", "name": "b", "at_sec": 20, "damage": True, "effects": [
            {"signal": "flow", "shape": "ramp", "z": 4, "rise_sec": 20},
            {"signal": "flow", "shape": "spike", "z": 9, "rise_sec": 1, "decay_sec": 100}]}]))
    stop = 30                                                       # S2 ramp 절반(2) 에서 멈춤, S1 은 2
    held = 2.0
    spike = 9 * (1 - 39 / 100)                                      # 60초의 spike (시작 20 + 상승 1, 39초 가라앉음)
    assert tag_effect(tp, stop + RECOVERY_SEC / 2, stop)[0] == pytest.approx(held + 1.0 + spike / 2)  # S1·spike 는 절반 회복, 손상은 그대로
    assert tag_effect(tp, stop + 10_000, stop)[0] == pytest.approx(held)                     # 안 고치면 손상 ramp 만 계속 남는다
    repair = 500
    assert tag_effect(tp, repair + RECOVERY_SEC / 2, stop, repair=repair)[0] == pytest.approx(held / 2)
    assert tag_effect(tp, repair + RECOVERY_SEC, stop, repair=repair)[0] == 0.0


def test_holds_damage_only_if_damage_started_before_stop():
    from simulator.fault import FaultEvent, holds_damage
    m = _mode([{"id": "S1", "name": "a", "at_sec": 0, "effects": [{"signal": "flow", "shape": "ramp", "z": 2, "rise_sec": 10}]},
               {"id": "S2", "name": "b", "at_sec": 100, "damage": True,
                "effects": [{"signal": "flow", "shape": "ramp", "z": 4, "rise_sec": 20}]}])
    tags = {("P-8101A", "FI-8101A"): plan_one(m)}
    assert not holds_damage(tags, FaultEvent("e", "P-8101A", m, 1000.0))                    # 안 멈췄다
    assert not holds_damage(tags, FaultEvent("e", "P-8101A", m, 1000.0, stop=1100.0))        # 손상 단계 시작 시각에 멈췄다
    assert holds_damage(tags, FaultEvent("e", "P-8101A", m, 1000.0, stop=1101.0))
    assert holds_damage(tags, FaultEvent("e", "P-8101A", m, 1000.0, stop=1011.0, speed=10))  # ×10 이면 S2 는 10초에 시작
