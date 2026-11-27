"""PU-D6 CDU 경유 제품 측 합성 완충 대용모델 (simulator.dynamic_buffer)."""
import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from simulator.calc_api import create_app
from simulator.dynamic_buffer import BufferModel, DynamicRegistry
from simulator.static_calc import ROOT, CalcError, Registry

REG = Registry(plants=("cdu", "hou"))
DYN = DynamicRegistry(REG)
MODEL = DYN.get("cdu")
STOP = {"stops": [{"equip_id": "P-8101B"}]}
SCENARIOS = {
    "normal": {"timeline": [], "duration_sec": 1800},
    "short": {"timeline": [{"at_sec": 300, "changes": STOP}, {"at_sec": 480, "changes": {}}], "duration_sec": 3600},
    "sustained": {"timeline": [{"at_sec": 300, "changes": STOP}], "duration_sec": 7200},
    "recovery": {"timeline": [{"at_sec": 300, "changes": STOP}, {"at_sec": 2100, "changes": {}}], "duration_sec": 7200},
}
RUNS = {name: MODEL.simulate(req) for name, req in SCENARIOS.items()}
MU = REG.get("cdu").control[("P-8401A", "FI-8401A")][0]


def flows(run):
    return [s["tags"]["FI-8401A"] for s in run["samples"]]


def first(run, tag, to):
    return next((e["t_sec"] for e in run["events"] if e.get("tag_id") == tag and e["to"] == to), None)


def variant(tmp_path: Path, **control) -> BufferModel:
    data = yaml.safe_load((ROOT / "config" / "dynamic_cdu_buffer.yaml").read_text())
    for section, values in control.items():
        data[section].update(values)
    path = tmp_path / "buffer.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True))
    return BufferModel(REG.get("cdu"), path)


def test_only_cdu_has_a_buffer_and_normal_point_matches_the_equivalent_circuit():
    assert list(DYN.models) == ["cdu"]
    d = MODEL.describe()
    assert d["alpha"] == pytest.approx(210.108 / 802.694, rel=1e-4)
    assert d["normal_out_m3_h"] == pytest.approx(MU, abs=0.01)
    run = RUNS["normal"]
    assert all(s["level_m"] == pytest.approx(2.0, abs=1e-9) for s in run["samples"])
    assert all(f == pytest.approx(MU, abs=1e-6) for f in flows(run))
    assert not run["events"]


@pytest.mark.parametrize("name", SCENARIOS)
def test_volume_is_conserved(name):
    b = RUNS[name]["balance"]
    assert abs(b["residual_m3"]) < 1e-8
    assert b["final_volume_m3"] == pytest.approx(b["initial_volume_m3"] + b["inflow_m3"] - b["outflow_m3"] - b["overflow_m3"], abs=1e-8)


def test_same_request_same_result():
    req = SCENARIOS["short"]
    assert json.dumps(MODEL.simulate(req), sort_keys=True) == json.dumps(RUNS["short"], sort_keys=True)


def test_one_pump_stop_is_buffered_at_first():
    """P-8101B 정지 직후 제품 유입은 약 82 m3/h 줄지만 P-8401A 유량은 용기가 받쳐 서서히 바뀐다(즉시 등가와 다르다)."""
    run = RUNS["sustained"]
    at = {s["t_sec"]: s for s in run["samples"]}
    assert at[300]["product_in_m3_h"] == pytest.approx(MODEL.alpha * at[300]["feed_m3_h"])
    assert at[290]["product_in_m3_h"] - at[300]["product_in_m3_h"] > 80
    assert MU - at[360]["tags"]["FI-8401A"] < 10
    seg = run["segments"][1]
    assert seg["instant"]["FI-8401A"] == pytest.approx(198.89, abs=0.05)       # M1 즉시 등가 전파


def test_short_reduction_does_not_alarm_the_product_pump():
    run = RUNS["short"]
    assert first(run, "FI-8401A", "warn") is None and first(run, "FI-8401A", "alarm") is None
    assert first(run, "FI-P8101B-A", "alarm") == 300                          # 정지 펌프 자신은 즉시 경보
    assert min(flows(run)) > 151


def test_sustained_reduction_alarms_independently_after_a_delay():
    run = RUNS["sustained"]
    warn, alarm = first(run, "FI-8401A", "warn"), first(run, "FI-8401A", "alarm")
    assert 300 < warn < alarm
    assert flows(run)[-1] == pytest.approx(MODEL.alpha * run["segments"][1]["feed_m3_h"], rel=0.01)
    assert not [e for e in run["events"] if e["kind"] == "boundary"]
    assert min(s["level_m"] for s in run["samples"]) > MODEL.cfg.vessel.min_level_m


def test_recovery_returns_to_normal_stage_and_setpoint():
    run = RUNS["recovery"]
    assert first(run, "FI-8401A", "normal") is not None
    last = run["samples"][-1]
    # 재기동 5100 초 뒤: 적분 제어(1200 초)라 액위가 설정값 위에서 천천히 내려온다
    assert last["level_m"] == pytest.approx(2.0, abs=0.15)
    assert last["tags"]["FI-8401A"] == pytest.approx(MU, rel=0.03)
    assert last["level_m"] < max(s["level_m"] for s in run["samples"])


def test_low_level_boundary_without_control(tmp_path):
    model = variant(tmp_path, control={"mode": "manual"})
    run = model.simulate(SCENARIOS["sustained"])
    low = [e for e in run["events"] if e["kind"] == "boundary"]
    assert low and low[0]["boundary"] == "low_level"
    starved = [s for s in run["samples"] if s["starved"]]
    assert starved and all(s["tags"]["FI-8401A"] is None for s in starved)
    assert min(s["level_m"] for s in run["samples"]) >= model.cfg.vessel.min_level_m - 1e-9
    assert abs(run["balance"]["residual_m3"]) < 1e-8


def test_overflow_boundary_is_counted(tmp_path):
    model = variant(tmp_path, control={"mode": "manual"}, vessel={"area_m2": 0.5})
    more = {"causes": [{"parameter": "P-8101A/speed_ratio", "value": 1.05}, {"parameter": "P-8101B/speed_ratio", "value": 1.05}]}
    run = model.simulate({"timeline": [{"at_sec": 60, "changes": more}], "duration_sec": 1800})
    assert any(e.get("boundary") == "overflow" for e in run["events"])
    assert run["balance"]["overflow_m3"] > 0
    assert max(s["level_m"] for s in run["samples"]) <= model.cfg.vessel.max_level_m + 1e-9
    assert abs(run["balance"]["residual_m3"]) < 1e-8


def test_scope_and_input_errors():
    with pytest.raises(CalcError) as e:
        MODEL.simulate({"timeline": [{"at_sec": 10, "changes": {"faults": [{"equip_id": "P-8401A", "mode": "PM-IMPELLER-WEAR"}]}}]})
    assert e.value.code == "unsupported"
    for bad in ({"timeline": [{"at_sec": 10}, {"at_sec": 5}]}, {"duration_sec": 0}, {"x": 1}, {"timeline": [{"at_sec": 4000}]}):
        with pytest.raises(CalcError) as e:
            MODEL.simulate(bad)
        assert e.value.code == "input_error"
    with pytest.raises(CalcError) as e:
        MODEL.simulate({"duration_sec": 99999})
    assert e.value.code == "out_of_range"
    with pytest.raises(CalcError) as e:       # 구간 정적 계산의 오류는 그대로 전달한다
        MODEL.simulate({"timeline": [{"at_sec": 10, "changes": {"stops": [{"equip_id": "P-8101B", "handling": "block"}]}}]})
    assert e.value.code == "input_error"


def test_static_calculation_is_unchanged():
    c = REG.get("cdu")
    from simulator.static_calc import flow_control_off
    with flow_control_off():                  # D6 가 받는 개방 회로 정적 값
        r = c.calculate(STOP)
    assert {t["tag_id"]: t["value"] for t in r["tags"] if t["equip_id"] == "P-8401A"}["FI-8401A"] == pytest.approx(198.89, abs=0.05)
    assert "mode" not in r
    held = c.calculate(STOP)                  # 화면 정적 계산(유량 제어 가정): 제품 분기 FIC 가 유량을 지킨다(2026-11-26)
    assert {t["tag_id"]: t["value"] for t in held["tags"] if t["equip_id"] == "P-8401A"}["FI-8401A"] == pytest.approx(MU, abs=0.05)


def test_http_dynamic_is_separate_from_static():
    client = TestClient(create_app(REG, token="t1", dynamic=DYN))
    h = {"X-Calc-Token": "t1"}
    assert client.post("/v1/plants/cdu/dynamic", json={}).status_code == 401
    model = client.get("/v1/plants/cdu/dynamic", headers=h).json()
    assert model["mode"] == "dynamic" and model["buffer"]["provenance"] == "synthetic assumption"
    run = client.post("/v1/plants/cdu/dynamic", headers=h, json=SCENARIOS["short"])
    assert run.status_code == 200 and run.json()["mode"] == "dynamic"
    assert client.post("/v1/plants/hou/dynamic", headers=h, json={}).status_code == 422
    assert client.post("/v1/plants/xx/dynamic", headers=h, json={}).status_code == 404
