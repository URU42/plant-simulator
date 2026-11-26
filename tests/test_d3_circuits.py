"""D3: 다섯 대표 결합 회로. 단계(정상·약함·심화·해제·수리), 전방 고장의 후속 변화·무변화, 독립 판정, 허위 연결 거절."""
from functools import lru_cache

import pytest

from common.config_models import load_yaml
from scripts.calibrate_physics import lower_target
from scripts.twin_expectations import PLANTS
from scripts.verify_circuits import CHANGED, CONFIG, MODES, cases, judge, open_plant, run_case, scope
from simulator.fault import plan
from simulator.physics_adapter import CoupledCase

PAIRS = {"cdu": ("P-8101A", "P-8401A"), "hou": ("P-2101", "P-2701"), "rfcc": ("P-1101A", "P-1303A"),
         "rhds": ("P-3101", "P-3102"), "vrds": ("P-4101", "P-4102")}
CASES = cases()


@lru_cache(maxsize=None)
def result(key, equip, mode):
    return {p["phase"]: p for p in run_case(key, equip, mode, 1)["phases"]}


@pytest.mark.parametrize("key", PLANTS)
def test_scope_matches_code_and_master(key):
    s = scope(key)
    assert (s["source"], s["downstream"]) == PAIRS[key] and set(s["computed_equipment"]) == {*PAIRS[key], s["second"]}
    assert {t.split("/")[0] for t in s["computed_tags"]} == {*PAIRS[key], s["second"]}
    inside = {(e["from"], e["to"]) for e in s["lumped_path"]}
    assert not inside & {tuple(e[:2]) for e in s["graph_only_edges"]}
    plant, _, physics, a = open_plant(key)
    assert all(x.startswith("virtual:") for x in s["virtual_elements"])
    control = plant.control_by_key()
    for tag, value in a.sensor_centers(a.solve(0, 0, 0)).items():        # 정상 해는 마스터 운전점
        assert value == pytest.approx(control[tag][0], abs=0.1 * control[tag][1])


@pytest.mark.parametrize("key", PLANTS)
def test_heat_exchanger_sides_returns_and_utilities_are_not_crossed(key):
    """경로는 공정 간선만 쓰고, 열교환기는 들어온 측으로 나가며, 환류·재순환·윤활·가스·촉매 간선을 닫지 않는다."""
    s = scope(key)
    plant = open_plant(key)[0]
    stream = {(a, b): x for a, b, _, x in plant.flow.edges}
    path = [(e["from"], e["to"]) for e in s["lumped_path"]]
    assert all(stream[e] == "process" for e in path)
    for first, second in zip(s["lumped_path"], s["lumped_path"][1:]):
        if first["to"] == "HX":
            assert first["side"] == second["side"] and first["side"] in ("저온측", "고온측")
    equipment = {e.equip_id for e in plant.equipment}
    assert not set(s["lumped_nodes_not_computed"]) & equipment
    assert not any(a == PAIRS[key][1] for a, _ in path), "후속 펌프에서 되돌아가는 간선은 회로에 없다"


def test_crossing_exchanger_sides_is_rejected():
    _, _, _, a = open_plant("hou")
    short = ["P-2101", "P-2101-S2", "MIX", "HX", "SEP", "STRIP", "FRAC", "P-2701"]      # 저온측 → 고온측: 간선은 모두 있지만 반응부를 건너뛴다
    with pytest.raises(ValueError, match="저온측"):
        a._check_path(short, set())
    with pytest.raises(ValueError, match="계측 설비"):
        a._check_path(["P-2101", "C-2201", "P-2701"], {"C-2201"})
    a._check_path(a.case.downstream_path, set())
    assert "P-2101-S2" in a.case.downstream_path


@pytest.mark.parametrize("key", PLANTS)
def test_every_parameter_is_graded_from_weak_to_severe(key):
    """막힘·마모·밸브는 약한 크기부터 단조로 커지고, 약한 크기에서 유량이 무너지지 않는다."""
    a = open_plant(key)[3]
    # 고장 펌프(둘째 펌프 파라미터 5·6 은 둘째)와 후속 펌프의 유량. 병렬의 다른 펌프는 부하를 받아 늘어난다.
    def flow(v, pump):
        c = a.sensor_centers(a.solve(*v))
        return c[a.keys[pump]["flow"]], c[a.keys[a.downstream]["flow"]]
    drum = next(b for b in a.case.network.branches if b.branch_id == a.case.downstream_branch).drum is not None
    for i in range(7):
        faulted = a.second if i >= 5 else a.primary
        base = flow((0,) * 7, faulted)
        series = [flow(tuple(x if j == i else 0 for j in range(7)), faulted) for x in (0, 0.1, 0.25, 0.5, 1)]
        for pump in range(2):
            values = [row[pump] for row in series]
            if pump == 0 and i in (3, 4) and drum:
                # 후속 펌프 흡입 드럼(RFCC·HOU)이 앞뒤를 끊는다: 후속 펌프 고장은 앞 펌프 유량을 바꾸지 않는다(액위가 받는다).
                assert values == pytest.approx([base[pump]] * len(values), rel=1e-9)
                continue
            assert values == sorted(values, reverse=True) and values[-1] < values[0]
            assert values[1] > 0.9 * base[pump], f"파라미터 {i}: 10 % 크기에서 유량이 무너진다"


@pytest.mark.parametrize("key", [k for k in PLANTS if k != "cdu"])
def test_booster_strainer_limit_is_calibrated_not_search_ceiling(key):
    case = CoupledCase.model_validate(load_yaml(CONFIG / f"physics_{key}_integrated.yaml"))
    assert case.max_booster_strainer_resistance_pa_s2_m6 < 1e11
    plant, _, _, a = open_plant(key)
    tag = a.keys[a.downstream]["flow"]
    limits = plant.tag_by_key()[tag].limits.model_dump(exclude_none=True)
    target = lower_target(limits, plant.control_by_key()[tag][0])
    assert a.sensor_centers(a.solve(0, 0, 0, 1, 0))[tag] >= target - 0.01


@pytest.mark.parametrize("key,equip,mode", CASES)
def test_phases_balance_outside_and_recovery(key, equip, mode):
    r = result(key, equip, mode)
    damage = any(s.damage for s in MODES[mode].stages)
    for phase, p in r.items():
        assert p["outside_changed"] == [], "회로 밖 설비·태그(환류·가스·윤활 포함)는 움직이지 않는다"
        assert abs(p["residuals"]["mass_kg_s"]) < 1e-6 and abs(p["residuals"]["pump_pa"]) < 1 and p["residuals"]["branch_pa"] < 1
    assert not any(r["normal"]["delta_sigma"].values())
    peak = lambda p: max(abs(v) for v in r[p]["delta_sigma"].values())
    assert peak("severe") > peak("weak") > 0
    assert (peak("released") == 0) != damage
    if damage:
        assert peak("released") == pytest.approx(peak("severe"), rel=0.02) and peak("repaired") == 0
    for p in ("normal", "repaired" if damage else "released"):
        assert all(j["level"] == "normal" for j in r[p]["judgement"].values())
    assert all(j["level"] == "normal" for j in r["weak"]["judgement"].values()), "약한 크기는 한계에 닿지 않는다"


@pytest.mark.parametrize("key", PLANTS)
def test_front_fault_changes_rear_by_solving_and_unchanged_cases_exist(key):
    source, rear = PAIRS[key]
    flow = "/".join(open_plant(key)[3].keys[rear]["flow"])
    assert result(key, source, "PM-IMPELLER-WEAR")["severe"]["delta_sigma"][flow] < (-1 if key == "cdu" else -CHANGED)     # 계산으로 실제 감소. CDU 는 병렬 펌프가 받아 화면 기준보다 작다
    weak = result(key, source, "PM-SUCTION-LOSS")["weak"]
    assert [t for t in weak["linked_changed"] if t.startswith(rear)] == [] and weak["linked_unchanged"], "약한 전방 막힘에서는 후속 변화가 잡음 수준이다"
    assert abs(weak["delta_sigma"][flow]) < CHANGED


@pytest.mark.parametrize("key,equip,mode", [c for c in CASES if MODES[c[2]].label.code == 1])
def test_normal_operation_change_raises_no_limit(key, equip, mode):
    for p in result(key, equip, mode).values():
        assert all(j["level"] == "normal" for j in p["judgement"].values())


@pytest.mark.parametrize("key", PLANTS)
def test_each_equipment_is_judged_by_its_own_tags_only(key):
    """다른 설비 값을 어떻게 바꿔도 한 설비의 판정은 같고, 전방만·후속만 이탈하는 경우가 모두 있다."""
    plant = open_plant(key)[0]
    source, rear = PAIRS[key]
    p = result(key, source, "PM-SUCTION-LOSS")["severe"]
    means = {tuple(k.split("/")): v for k, v in p["mean"].items()}
    for me, other in ((source, rear), (rear, source)):
        wrecked = {k: (0.0 if k[0] == other else v) for k, v in means.items()}
        alone = {k: v for k, v in means.items() if k[0] == me}
        assert judge(plant, me, wrecked) == judge(plant, me, means) == judge(plant, me, alone)
    assert p["judgement"][source]["level"] != "normal" and p["judgement"][rear]["level"] == "normal"
    assert result(key, rear, "PM-SUCTION-LOSS")["severe"]["judgement"][rear]["level"] != "normal"


def test_results_depend_on_the_circuit_not_on_fixed_outputs():
    """기존 수치는 강제값이 아니다: 경계를 바꾸면 해도 바뀐다."""
    a = open_plant("cdu")[3]
    moved = a.case.model_copy(update={"default_point": a.case.default_point.model_copy(
        update={"suction_abs_pa": a.case.default_point.suction_abs_pa * 1.1})})
    flow = lambda c: next(b.flow_m3_s for b in c.solve(1.0).branches if b.branch_id == c.downstream_branch)
    assert flow(moved) != pytest.approx(flow(a.case), rel=1e-5)


@pytest.mark.parametrize("key", PLANTS)
def test_twin_has_no_graph_propagation_but_base_runtime_keeps_it(key):
    """트윈에서는 어떤 모드도 공정 그래프를 따라 다른 설비를 움직이지 않는다(축 전파는 남는다). 기본 생성기는 그대로다."""
    plant, _, physics, _ = open_plant(key)
    control, graph = plant.control_by_key(), False
    for e in plant.equipment:
        for mode in MODES.values():
            if mode.applies_to(e):
                tags = physics.tag_plan(mode, e.equip_id)
                assert {tp.via for k, tp in tags.items() if k[0] != e.equip_id} <= {"physics", "driven", "utility"}
                graph |= not mode.physics_only and any(tp.via == "process" for tp in plan(plant, mode, e.equip_id, control).values())
    assert graph, "기본 생성기의 하류 전파는 바꾸지 않는다"
    if key == "rfcc":
        surge = physics.tag_plan(MODES["CC-SURGE-HIGH-RATIO"], "C-1401")
        assert surge and {k[0] for k in surge} == {"C-1401"}
