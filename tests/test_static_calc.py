"""PU-M1 시뮬레이션 전용 정적 계산 (simulator.static_calc, simulator.calc_api)."""
import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from simulator.calc_api import create_app
from simulator.fault import HELD
from simulator.physics_adapter import PhysicsAdapter
from simulator.static_calc import PLANTS, CalcError, PlantCalc, Registry

REG = Registry()
_OPEN: dict[str, PlantCalc] = {}


def open_loop(plant: str) -> PlantCalc:
    """유량 제어 가정을 끈 계산기(엔진 회로 해와 직접 대조하는 검사용)."""
    if plant not in _OPEN:
        _OPEN[plant] = PlantCalc(plant, flow_control=False)
    return _OPEN[plant]


def calc(plant, **req):
    return REG.get(plant).calculate(req)


def by_tag(result):
    return {(t["equip_id"], t["tag_id"]): t for t in result["tags"]}


def changed(result):
    return {k for k, t in by_tag(result).items() if t["status"] == "changed"}


def error(plant, **req) -> CalcError:
    with pytest.raises(CalcError) as exc:
        calc(plant, **req)
    return exc.value


@pytest.mark.parametrize("plant", PLANTS)
def test_no_change_is_the_normal_operating_point(plant):
    c = REG.get(plant)
    r = calc(plant)
    assert not changed(r)
    assert all(e["stage"] == "normal" for e in r["equipment"])
    for k, t in by_tag(r).items():
        assert t["value"] == pytest.approx(c.control[k][0], rel=1e-9, abs=1e-9), k
        assert t["normal"] == t["value"]


@pytest.mark.parametrize("plant", PLANTS)
def test_same_input_same_result_and_reset_restores_normal(plant):
    c = REG.get(plant)
    source = next(a.primary for a in c.adapters.values() if isinstance(a, PhysicsAdapter))
    req = {"causes": [{"parameter": f"{source}/speed_ratio", "value": 0.9}],
           "faults": [{"equip_id": source, "mode": "PM-IMPELLER-WEAR"}]}
    first = c.calculate(req)
    assert json.dumps(first, sort_keys=True) == json.dumps(c.calculate(req), sort_keys=True)
    assert changed(first)
    # 원복: 원인 값을 정상값으로 되돌리면 정상 운전점과 같은 값
    back = c.calculate({"causes": [{"parameter": f"{source}/speed_ratio", "value": 1.0}]})
    assert [t["value"] for t in back["tags"]] == [t["value"] for t in calc(plant)["tags"]]


@pytest.mark.parametrize("plant", PLANTS)
def test_physical_fault_matches_the_engine_solution_at_full_amount(plant):
    """정적 대표값(마지막 단계까지 끝까지 진행) = 엔진 회로 해(PhysicsAdapter.solve)의 계측 환산."""
    c = REG.get(plant)
    a = next(a for a in c.adapters.values() if isinstance(a, PhysicsAdapter))
    r = by_tag(open_loop(plant).calculate({"faults": [{"equip_id": a.second, "mode": "PM-SUCTION-LOSS"}]}))
    normal, state = a.sensor_centers(a.solve()), a.sensor_centers(a.solve(0, 0, 0, 0, 0, 1, 0))
    for k in a.tag_keys:
        assert r[k]["delta"] == pytest.approx(state[k] - normal[k], abs=1e-9), k
        assert r[k]["basis"] == "physics"


@pytest.mark.parametrize("plant", PLANTS)
def test_circuit_change_does_not_move_equipment_outside_the_circuit(plant):
    c = REG.get(plant)
    for a in c.adapters.values():
        name = "speed_ratio"
        r = calc(plant, causes=[{"parameter": f"{a.primary}/{name}", "value": 0.95}])
        # 결합 회로 유량이 바뀌면 고정 분배 대용모델로 하류 단일 펌프 회로만 함께 움직인다(M2, 2026-11-26)
        split = {c.adapters[s].primary for s, src in c.split_from.items() if src == a.circuits()[0]["id"]}
        assert {k[0] for k in changed(r)} <= set(a.roles) | split, a.primary
        for b in r["circuits"]:
            assert abs(b["mass_residual_kg_s"]) < 1e-6
            assert abs(b["pressure_residual_pa"]) < 1.0


def test_stage_choice_uses_the_hydraulic_amount_up_to_that_stage():
    c = REG.get("rhds")
    a = c.adapters["coupled:P-3101"]
    s2 = by_tag(open_loop("rhds").calculate({"faults": [{"equip_id": "P-3101", "mode": "PM-SUCTION-LOSS", "stage": "S2"}]}))
    expected = a.sensor_centers(a.solve(0.45))
    normal = a.sensor_centers(a.solve())
    for k in a.tag_keys:
        assert s2[k]["delta"] == pytest.approx(expected[k] - normal[k], abs=1e-9)


def test_series_stop_needs_an_explicit_handling_and_is_reported_as_assumption():
    e = error("hou", stops=[{"equip_id": "P-2101-S2"}])
    assert e.code == "input_error" and "기본값이 없습니다" in e.message
    assert error("hou", stops=[{"equip_id": "P-2101-S2", "handling": "pass"}]).code == "input_error"
    r = calc("hou", stops=[{"equip_id": "P-2101-S2", "handling": "bypass", "resistance_ratio": 0.02}])
    assert [a["kind"] for a in r["assumptions"]] == ["series_stop", "flow_control", "fixed_split"]   # 제어 포화 → 하류 P-2601 고정 분배
    assert by_tag(r)[("P-2101-S2", "FI-2101S2")]["value"] == pytest.approx(0.0, abs=1e-9)
    blocked = error("hou", stops=[{"equip_id": "P-2101", "handling": "block"}])
    assert blocked.code == "no_solution" and blocked.detail["reason"] == "blocked"


def test_parallel_stop_takes_no_handling():
    assert error("cdu", stops=[{"equip_id": "P-8101B", "handling": "block"}]).code == "input_error"
    r = by_tag(calc("cdu", stops=[{"equip_id": "P-8101B"}]))
    assert r[("P-8101A", "FI-8101A")]["delta"] > 0       # 남은 펌프가 더 많이 보낸다


def test_combination_limits_and_conflicts():
    two = [{"equip_id": "P-3101", "mode": "PM-IMPELLER-WEAR"}, {"equip_id": "P-3102", "mode": "PM-SUCTION-LOSS"}]
    assert error("rhds", faults=two).code == "conflict"
    assert error("rhds", causes=[{"parameter": "P-3101/strainer_blockage", "value": 0.5}],
                 faults=[{"equip_id": "P-3101", "mode": "PM-SUCTION-LOSS"}]).code == "conflict"
    assert error("rhds", causes=[{"parameter": "coupled:P-3101/control_valve_opening", "value": 0.9}],
                 faults=[{"equip_id": "P-3101", "mode": "OP-THROUGHPUT-CHANGE"}]).code == "conflict"
    over = {"equip_id": "P-3101", "tag_id": "FI-P3101", "value": 1.0}
    assert error("rhds", sensor_overrides=[over, over]).code == "conflict"
    assert error("rhds", causes=[{"parameter": "P-3101/speed_ratio", "value": 2.0}]).code == "out_of_range"
    assert error("rhds", causes=[{"parameter": "P-3101/nope", "value": 1.0}]).code == "input_error"
    assert error("rhds", causes=[{"parameter": "P-3101/speed_ratio", "value": "1"}]).code == "input_error"
    # 다른 회로·회로 밖 설비의 고장은 함께 계산한다
    r = calc("rhds", faults=[{"equip_id": "P-3101", "mode": "PM-IMPELLER-WEAR"}, {"equip_id": "F-3101", "mode": "FN-IMPELLER-FOULING"}])
    assert {"P-3101", "F-3101"} <= {k[0] for k in changed(r)}


def test_transient_and_excluded_modes_are_unsupported():
    assert error("rhds", faults=[{"equip_id": "C-3101", "mode": "CC-SURGE"}]).code == "unsupported"
    assert error("rhds", faults=[{"equip_id": "P-3102", "mode": "PM-SUCTION-HOT"}]).code == "unsupported"
    catalog = REG.get("rhds").catalog()
    rules = {i["rule"]: i for i in catalog["interactions"]}
    assert rules["I5"]["supported"] is False and rules["I3"]["supported"] is True


def test_sensor_override_changes_only_that_value():
    r = calc("rhds", sensor_overrides=[{"equip_id": "P-3101", "tag_id": "FI-P3101", "value": 1.0}])
    assert changed(r) == {("P-3101", "FI-P3101")}
    t = by_tag(r)[("P-3101", "FI-P3101")]
    assert t["basis"] == "sensor_override" and t["value"] == 1.0


def test_interactions_I4_speed_and_fouling_amplify_vibration():
    fouled = by_tag(calc("rhds", faults=[{"equip_id": "F-3101", "mode": "FN-IMPELLER-FOULING"}]))
    faster = by_tag(calc("rhds", faults=[{"equip_id": "F-3101", "mode": "FN-IMPELLER-FOULING"}],
                         causes=[{"parameter": "F-3101/speed_ratio", "value": 1.05}]))
    k = ("F-3101", "VI-A-F3101")
    assert faster[k]["delta"] > fouled[k]["delta"] > 0


def test_process_propagation_is_reported_as_not_computed():
    r = by_tag(calc("rhds", faults=[{"equip_id": "M-3101", "mode": "OP-LOAD-CHANGE"}]))
    missing = [t for t in r.values() if t["status"] == "not_computed"]
    assert missing and all(t["value"] is None and t["basis"] == "not_modeled" for t in missing)


def test_static_values_skip_transient_shapes():
    assert HELD == ("step", "ramp")


# --- HTTP ----------------------------------------------------------------------------------
def test_http_catalog_and_calculate_without_collector():
    client = TestClient(create_app(REG, token="t0"))
    assert client.get("/health").status_code == 200
    assert client.get("/v1/plants/hou/catalog").status_code == 401
    h = {"X-Calc-Token": "t0"}
    cat = client.get("/v1/plants/hou/catalog", headers=h).json()
    text = json.dumps(cat, ensure_ascii=False)
    assert '"label"' not in text and '"expect"' not in text and "t0" not in text      # 정답지·비밀값 없음
    for m in REG.get("hou").modes.values():
        assert m.label.cause not in text and m.label.action not in text
    assert cat["stop_options"]["series"]["default"] is None
    ok = client.post("/v1/plants/hou/calculate", headers=h, json={"faults": [{"equip_id": "P-2101", "mode": "PM-SUCTION-LOSS"}]})
    assert ok.status_code == 200 and ok.json()["synthetic"] is True and "X-Calc-Elapsed-Ms" in ok.headers
    assert client.post("/v1/plants/xx/calculate", headers=h, json={}).status_code == 404
    bad = client.post("/v1/plants/hou/calculate", headers=h, json={"stops": [{"equip_id": "P-2101"}]})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "input_error"
    no = client.post("/v1/plants/hou/calculate", headers=h, json={"stops": [{"equip_id": "P-2101", "handling": "block"}]})
    assert no.status_code == 422 and no.json()["error"]["detail"]["reason"] == "blocked"


def test_service_does_not_import_collector_storage_or_streams():
    code = ("import sys, simulator.calc_api; "
            "print(sorted(m for m in sys.modules if m.split('.')[0] in ('collector', 'redis', 'sqlite3', 'psycopg')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "[]"


# --- M2 하류 전파: 고정 분배 대용모델과 정적 계산 전용 단일 회로 (2026-11-26) -----------------------------
EXTRA = {"cdu": ["P-8102A", "P-8301A", "P-8302A"], "hou": ["P-2501", "P-2601"],
         "rfcc": ["P-1301A", "P-1302A", "P-1402A", "P-1403A", "P-1501A", "P-1502A"], "rhds": ["P-3103"], "vrds": ["P-4103"]}


@pytest.mark.parametrize("plant", PLANTS)
def test_calc_only_circuits_do_not_enter_the_feeder_twin(plant):
    from common.config_models import load_plant
    from simulator.physics_adapter import load_physics
    from simulator.static_calc import ROOT
    bundle = load_physics(load_plant(ROOT / f"config/plant_{plant}.yaml"), ROOT / f"config/physics_{plant}_twin.yaml")
    assert not set(EXTRA[plant]) & set(bundle.roles)          # 수집기 트윈은 그대로
    c = REG.get(plant)
    assert {a.primary for a in c.extra} == set(EXTRA[plant])
    for e in EXTRA[plant]:                                   # 각 펌프에 바꿀 수 있는 운전값이 생겼다
        assert f"{e}/speed_ratio" in c.params


@pytest.mark.parametrize("plant", PLANTS)
def test_front_pump_change_moves_downstream_pumps_by_fixed_split(plant):
    c = open_loop(plant)        # 고정 분배 규칙 자체를 본다(유량 제어가 없으면 총유량이 그대로 변한다)
    source = next(a for a in c.adapters.values() if isinstance(a, PhysicsAdapter))
    r = c.calculate({"causes": [{"parameter": f"{source.primary}/speed_ratio", "value": 0.9}]})
    total = next(b for b in r["circuits"] if b["circuit_id"] == source.circuit_of(source.primary))["total_flow_m3_h"]
    f = total / c._normal_flow[source.circuit_of(source.primary)]
    assert f < 0.99
    tags = by_tag(r)
    split = [a for a in r["assumptions"] if a["kind"] == "fixed_split"]
    assert {a["equip_id"] for a in split} == {c.adapters[s].primary for s in c.split_from}
    for sid in c.split_from:
        a = c.adapters[sid]
        flow = tags[a.keys["flow"]]
        assert flow["value"] == pytest.approx(f * flow["normal"], rel=1e-6)       # 유량 = 상류 유량비 × 정상
        assert flow["basis"] == "empirical" and flow["status"] == "changed"
        assert tags[a.keys["discharge_pressure"]]["status"] == "changed"           # 압력은 펌프 곡선으로 함께 변한다
    connected = {x["equip_id"]: x["via"] for x in r["connected_equipment"]}
    assert all(connected[c.adapters[s].primary] == "empirical" for s in c.split_from)


def test_stopping_front_pump_raises_downstream_alarm_stage():
    r = calc("cdu", stops=[{"equip_id": "P-8101A"}])
    stages = {e["equip_id"]: e["stage"] for e in r["equipment"]}
    assert stages["P-8301A"] == stages["P-8302A"] == "alarm" and stages["P-8303A"] in ("warn", "alarm")   # 제어 포화(한 대로는 못 채움)


def test_downstream_pump_alone_keeps_its_own_physics():
    # 상류가 그대로면 고정 분배를 걸지 않는다: 하류 펌프 자신의 회전수 변화로 유량이 바뀐다
    r = calc("cdu", causes=[{"parameter": "P-8301A/speed_ratio", "value": 0.9}])
    assert not [a for a in r["assumptions"] if a["kind"] == "fixed_split"]
    assert by_tag(r)[("P-8301A", "FI-8301A")]["value"] < by_tag(r)[("P-8301A", "FI-8301A")]["normal"]
    assert by_tag(r)[("P-8301A", "FI-8301A")]["basis"] == "physics"


# --- 심각 구간 외삽·캐비테이션 붕괴 (2026-11-26 사용자 승인) -------------------------------------------------
def test_collapse_extrapolation_is_off_outside_the_calculator():
    from simulator.physics.npsh import collapse_extrapolation
    a = next(x for x in REG.get("rhds").adapters.values() if isinstance(x, PhysicsAdapter))
    npsh = a.case.station.units[0].npsh
    s = npsh.collapse_margin * 0.5
    assert npsh.head_loss(s) == npsh.max_head_loss                     # 기본(수집기 트윈): 상한에서 멈춘다
    with collapse_extrapolation():
        assert npsh.max_head_loss < npsh.head_loss(s) < 1
        assert npsh.head_loss(0.0) == 1.0
    assert npsh.head_loss(s) == npsh.max_head_loss


@pytest.mark.parametrize("plant", PLANTS)
def test_every_circuit_parameter_end_is_solvable_and_marked_when_extrapolated(plant):
    c = REG.get(plant)
    for p in c.params.values():
        if p.circuit is None:
            continue
        for v in (p.lo, p.hi):
            r = c.calculate({"causes": [{"parameter": p.id, "value": v}]})
            kinds = [a["kind"] for a in r["assumptions"]]
            assert ("extrapolated" in kinds) == p.extrapolated(v), (p.id, v)
        lo, hi = p.validated()
        assert p.lo <= lo and hi <= p.hi                  # 심각 범위는 검증 범위를 포함한다
        if (lo, hi) != (p.lo, p.hi):
            assert "검증 범위" in p.public()["note"]


@pytest.mark.parametrize("plant,pump", [("hou", "P-2101"), ("rhds", "P-3101"), ("vrds", "P-4101")])
def test_severe_front_pump_blockage_propagates_to_downstream_alarm(plant, pump):
    c = REG.get(plant)
    p = c.params[f"{pump}/strainer_blockage"]
    r = c.calculate({"causes": [{"parameter": p.id, "value": p.hi}]})
    kinds = [a["kind"] for a in r["assumptions"]]
    assert {"extrapolated", "cavitation_collapse", "fixed_split"} <= set(kinds)
    downs = {c.adapters[s].primary for s in c.split_from}
    assert {e["stage"] for e in r["equipment"] if e["equip_id"] in downs} & {"alarm", "trip"}


def test_parallel_spare_compensates_single_pump_but_not_both():
    one = calc("cdu", causes=[{"parameter": "P-8101A/strainer_blockage", "value": 8}])
    both = calc("cdu", causes=[{"parameter": "P-8101A/strainer_blockage", "value": 8},
                               {"parameter": "P-8101B/strainer_blockage", "value": 8}])
    stage = lambda r, e: next(x["stage"] for x in r["equipment"] if x["equip_id"] == e)      # noqa: E731
    assert stage(one, "P-8301A") == "normal"           # 짝 펌프가 넘겨받는다
    assert stage(both, "P-8301A") == "alarm"


# --- 계산 전용 고장 확장 (2026-11-26) -----------------------------------------------------------------
def test_calc_fault_extension_does_not_touch_twin_fault_modes():
    from pathlib import Path

    from simulator.fault import load_fault_modes
    shared = load_fault_modes(Path(__file__).resolve().parents[1] / "config" / "fault_modes.yaml")
    assert "PM-DISCHARGE-BLOCK-VALVE" not in shared                 # 수집기 트윈·AE 데이터가 읽는 파일은 그대로
    assert "P-8301A" not in shared["PM-IMPELLER-WEAR"].equip_ids
    assert shared["PM-DISCHARGE-VALVE-RESTRICTION"].equip_ids == ["P-8101A"]


@pytest.mark.parametrize("plant", PLANTS)
def test_every_supported_fault_calculates(plant):
    c = REG.get(plant)
    for e in c.catalog()["equipment"]:
        for f in e["faults"]:
            if not f["supported"]:
                continue
            for stage in f["stages"]:
                r = c.calculate({"faults": [{"equip_id": e["equip_id"], "mode": f["mode"], "stage": stage["id"]}]})
                assert r["tags"], (e["equip_id"], f["mode"], stage["id"])


@pytest.mark.parametrize("plant,pump", [("cdu", "P-8301A"), ("hou", "P-2601"), ("rfcc", "P-1501A"), ("rhds", "P-3103")])
def test_calc_only_single_pump_faults_use_physics(plant, pump):
    faults = {f["mode"]: f for e in REG.get(plant).catalog()["equipment"] if e["equip_id"] == pump for f in e["faults"]}
    assert faults["PM-SUCTION-LOSS"]["basis"] == "physics" and faults["PM-SUCTION-LOSS"]["parameters"] == ["strainer_blockage"]
    assert faults["PM-IMPELLER-WEAR"]["supported"] and faults["PM-IMPELLER-WEAR"]["basis"] == "physics"
    r = by_tag(calc(plant, faults=[{"equip_id": pump, "mode": "PM-IMPELLER-WEAR"}]))
    a = REG.get(plant).adapters[f"single:{pump}"]
    flow, out = r[a.keys["flow"]], r[a.keys["discharge_pressure"]]
    assert flow["basis"] == out["basis"] == "physics"
    assert flow["value"] == pytest.approx(flow["normal"], rel=1e-5)        # 유량 제어가 유량을 지킨다
    assert out["value"] < out["normal"]                                    # 양정이 줄어 토출압이 낮다


@pytest.mark.parametrize("plant,pump", [("hou", "P-2101"), ("rfcc", "P-1101A"), ("rhds", "P-3101"), ("vrds", "P-4101")])
def test_feed_pump_product_valve_fault_extended(plant, pump):
    f = next(f for e in REG.get(plant).catalog()["equipment"] if e["equip_id"] == pump
             for f in e["faults"] if f["mode"] == "PM-DISCHARGE-VALVE-RESTRICTION")
    assert f["supported"] and f["parameters"] == ["valve_restriction"]


def test_block_valve_fault_on_parallel_spare_pump():
    modes = {f["mode"] for e in REG.get("cdu").catalog()["equipment"] if e["equip_id"] == "P-8101B" for f in e["faults"]}
    assert "PM-DISCHARGE-BLOCK-VALVE" in modes
    s1 = calc("cdu", faults=[{"equip_id": "P-8101B", "mode": "PM-DISCHARGE-BLOCK-VALVE", "stage": "S1"}])
    s2 = calc("cdu", faults=[{"equip_id": "P-8101B", "mode": "PM-DISCHARGE-BLOCK-VALVE", "stage": "S2"}])
    flow = lambda r: next(t["value"] for t in r["tags"] if t["equip_id"] == "P-8101B" and t["signal"] == "flow")   # noqa: E731
    assert flow(s2) < flow(s1)
    assert "extrapolated" not in [a["kind"] for a in s1["assumptions"]]
    assert "extrapolated" in [a["kind"] for a in s2["assumptions"]]          # 개도 0.01 은 검증 하한 0.1 아래
    e = error("cdu", causes=[{"parameter": "P-8101B/discharge_valve_opening", "value": 0.5}],
              faults=[{"equip_id": "P-8101B", "mode": "PM-DISCHARGE-BLOCK-VALVE"}])
    assert e.code == "conflict"


# --- 유량 제어 가정 (2026-11-26 사용자 승인) ----------------------------------------------------------------
def test_flow_control_absorbs_one_parallel_pump_fault_within_valve_range():
    r = calc("cdu", causes=[{"parameter": "P-8101A/strainer_blockage", "value": 0.48}])
    ctl = [a for a in r["assumptions"] if a["kind"] == "flow_control"]
    assert ctl and not ctl[0]["saturated"] and 0.7 < ctl[0]["valve_opening"] < 1
    tags = by_tag(r)
    assert tags[("P-8101B", "FI-P8101B-A")]["delta"] > 0                   # 짝 펌프가 넘겨받는다
    for e in ("P-8301A", "P-8302A", "P-8303A"):                            # 하류 단일 펌프는 그대로
        assert all(t["status"] == "unchanged" for t in r["tags"] if t["equip_id"] == e), e
    assert tags[("P-8401A", "FI-8401A")]["value"] == pytest.approx(tags[("P-8401A", "FI-8401A")]["normal"], rel=1e-5)
    # (같은 회로의 후속 펌프는 헤더 압력이 조금 내려 토출압만 미세하게 변한다)
    assert not [a for a in r["assumptions"] if a["kind"] == "fixed_split"]
    # 제어가 없으면 같은 입력에서 하류가 변한다(제어 가정의 효과)
    o = open_loop("cdu").calculate({"causes": [{"parameter": "P-8101A/strainer_blockage", "value": 0.48}]})
    assert [a for a in o["assumptions"] if a["kind"] == "fixed_split"]


@pytest.mark.parametrize("plant", PLANTS)
def test_flow_control_keeps_the_normal_point(plant):
    r = calc(plant)
    assert not [a for a in r["assumptions"] if a["kind"] == "flow_control"]
    assert all(t["status"] == "unchanged" for t in r["tags"] if t["status"] != "not_computed")


def test_flow_control_saturates_and_downstream_changes_beyond_valve_range():
    r = calc("cdu", causes=[{"parameter": "P-8101A/strainer_blockage", "value": 8}])
    ctl = next(a for a in r["assumptions"] if a["kind"] == "flow_control")
    assert ctl["saturated"]
    assert [a for a in r["assumptions"] if a["kind"] == "fixed_split"]


def test_manual_product_valve_turns_flow_control_off():
    r = calc("cdu", causes=[{"parameter": "coupled:P-8101A/control_valve_opening", "value": 0.5}])
    assert not [a for a in r["assumptions"] if a["kind"] == "flow_control" and a["circuit_id"] == "coupled:P-8101A"]
    assert by_tag(r)[("P-8401A", "FI-8401A")]["delta"] < 0


def test_wear_severe_limit_is_one_and_a_half():
    for plant in PLANTS:
        for p in REG.get(plant).params.values():
            if p.name == "impeller_wear":
                assert p.hi <= 1.5 + 1e-9 and p.validated() == (0.0, 1.0), p.id
                assert p.unit == "배수(1 = 검증 최대)" and p.note.startswith("1 = 양정 −")


# --- D11 조치 효과 시뮬레이션 (2026-11-26) ---------------------------------------------------------------

@pytest.mark.parametrize("plant", PLANTS)
def test_every_circuit_reports_pump_head_and_shaft_power(plant):
    """D11: 회로의 모든 연결 펌프(병렬·후속)에 양정·축동력, 공통 헤더 압력을 낸다(정상점)."""
    r = calc(plant)
    for c in r["circuits"]:
        for u in c["units"]:
            if u["status"] == "running":
                assert u["shaft_kw"] > 0, (c["circuit_id"], u)
                if "head_m" in u:
                    assert u["head_m"] > 0
        if c["circuit_id"].startswith("coupled:"):
            assert c["header_kpa_abs"] > 0
            assert any(u.get("role") == "downstream" for u in c["units"]), c["circuit_id"]


def test_action_restore_is_consistent_with_the_normal_state():
    """회복 전후 일관성: 원인 값을 정상값으로 되돌린 조치 결과 = 아무 원인 없는 정상 결과. 상태가 남지 않는다."""
    normal = calc("cdu")
    calc("cdu", causes=[{"parameter": "P-8101A/strainer_blockage", "value": 0.9}])
    restored = calc("cdu", causes=[{"parameter": "P-8101A/strainer_blockage", "value": 0}])
    assert [t["value"] for t in restored["tags"]] == [t["value"] for t in normal["tags"]]
    assert restored["equipment"] == normal["equipment"]


def test_partial_action_lies_between_no_action_and_full_removal():
    base, half, full = (calc("cdu", causes=[{"parameter": "P-8101A/strainer_blockage", "value": v}]) for v in (0.9, 0.45, 0.0))
    unit = lambda r: next(u for c in r["circuits"] for u in c["units"] if u["equip_id"] == "P-8101A")  # noqa: E731
    assert unit(base)["flow_m3_h"] < unit(half)["flow_m3_h"] < unit(full)["flow_m3_h"]


def test_concurrent_requests_are_isolated():
    """동시 요청 격리: 서로 다른 입력을 동시에 보내도 각 결과가 순서대로 보낸 결과와 같다."""
    from concurrent.futures import ThreadPoolExecutor
    client = TestClient(create_app(Registry(), token="t0"))
    h = {"X-Calc-Token": "t0"}
    bodies = [{"causes": [{"parameter": "P-8101A/strainer_blockage", "value": v}]} for v in (0, 0.3, 0.6, 0.9)] * 3
    post = lambda b: client.post("/v1/plants/cdu/calculate", headers=h, json=b).json()    # noqa: E731
    alone = [post(b) for b in bodies[:4]]
    with ThreadPoolExecutor(8) as pool:
        together = list(pool.map(post, bodies))
    for i, r in enumerate(together):
        assert r["tags"] == alone[i % 4]["tags"] and r["circuits"] == alone[i % 4]["circuits"]


def test_suction_temperature_rise_moves_the_suction_fluid_temperature_tag():
    # 2026-11-27: 캐비테이션 모델이 있는 P-8303A 는 흡입 유체 온도(TI-8303B)를 물리 출력으로 낸다(원인 값 1 = +15 K).
    # 그 전에는 경험식 신호라 원인 값을 바꿔도 계산값이 그대로여서 진단이 가장 직접적인 증거를 쓰지 못했다.
    base = by_tag(calc("cdu"))[("P-8303A", "TI-8303B")]
    hot = by_tag(calc("cdu", causes=[{"parameter": "P-8303A/suction_temperature_rise", "value": 1.0}]))[("P-8303A", "TI-8303B")]
    assert base["basis"] == hot["basis"] == "physics"
    assert hot["value"] - base["value"] == pytest.approx(15.0, abs=0.05)
    assert by_tag(calc("cdu"))[("P-8303A", "TI-8303A")]["basis"] == "empirical"     # 베어링 온도는 그대로 경험식


def test_single_fan_speed_is_a_physical_signal_at_all_operating_points():
    key = ("F-8201", "SI-8201A")
    normal = by_tag(calc("cdu"))[key]
    slower = by_tag(calc("cdu", causes=[{"parameter": "F-8201/speed_ratio", "value": 0.95}]))[key]
    worn = by_tag(calc("cdu", causes=[{"parameter": "F-8201/impeller_wear", "value": 0.5}]))[key]
    assert normal["basis"] == slower["basis"] == worn["basis"] == "physics"
    assert slower["value"] == pytest.approx(normal["value"] * 0.95)
    assert worn["value"] == pytest.approx(normal["value"])


def test_fan_wear_reduces_flow_without_a_modeled_flow_controller():
    key = ("F-8201", "FI-8201A")
    normal = by_tag(calc("cdu"))[key]
    cause = [{"parameter": "F-8201/impeller_wear", "value": 0.6}]
    worn = by_tag(calc("cdu", causes=cause))[key]
    expected = by_tag(open_loop("cdu").calculate({"causes": cause}))[key]
    assert worn["value"] < normal["value"]
    assert worn["value"] == pytest.approx(expected["value"])
