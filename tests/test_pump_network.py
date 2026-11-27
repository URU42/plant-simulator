"""PU-D4 직렬·병렬 펌프 묶음 솔버."""
import math

import pytest
from pydantic import ValidationError

from scripts.verify_pump_network import PARALLEL, SERIES, UNITS, build, readings, run, scenarios, sigma
from simulator.physics.network import OperatingPoint
from simulator.physics.pump import GRAVITY_M_S2
from simulator.physics.station import SolveError, Stopped, UnitState

BUILT = {k: build(k) for k in UNITS}


def solve(key, states=None, **extra):
    st, a = BUILT[key]
    return st.solve(a.case.default_point, states, **extra)


def ids(key):
    return [u.unit_id for u in BUILT[key][0].units]


def downstream(key, r):
    return next(b for b in r.branches if b.branch_id == BUILT[key][1].case.downstream_branch)


@pytest.mark.parametrize("key", UNITS)
def test_normal_point_is_the_master_operating_point(key):
    st, a = BUILT[key]
    new = readings(a, solve(key))
    for name, value in new.items():
        eid, signal = name.split(":")
        assert abs(value - a.control[a.keys[eid][signal]][0]) < 0.01 * sigma(a, name), name
    assert len(new) == len(a.tag_keys)


@pytest.mark.parametrize("key", SERIES)
def test_series_same_flow_heads_add_and_stage_pressures_connect(key):
    st, _ = BUILT[key]
    r = solve(key)
    one, two = r.units
    assert one.flow_m3_s == two.flow_m3_s == r.flow_m3_s
    u = st.units[0]
    rho = st.demand.density_kg_m3
    line = u.line.resistance(rho) + u.check_valve.resistance(rho) + u.valve.resistance(rho, 1.0)
    assert two.suction_abs_pa == pytest.approx(one.discharge_abs_pa - line * r.flow_m3_s**2, abs=1e-3)
    rise = rho * GRAVITY_M_S2 * (one.head_m + two.head_m) - 2 * line * r.flow_m3_s**2
    assert r.header_abs_pa - one.suction_abs_pa == pytest.approx(rise, rel=1e-12)
    assert one.head_m == pytest.approx(two.head_m, rel=1e-9)


@pytest.mark.parametrize("key", PARALLEL)
def test_parallel_flows_add_at_common_pressures(key):
    r = solve(key, {ids(key)[0]: UnitState(head_loss=0.05)})
    one, two = r.units
    assert one.flow_m3_s + two.flow_m3_s == pytest.approx(r.flow_m3_s, rel=1e-12)
    assert one.flow_m3_s < two.flow_m3_s
    assert one.suction_abs_pa == two.suction_abs_pa


@pytest.mark.parametrize("key", UNITS)
def test_balances_for_every_solved_scenario_and_determinism(key):
    st, a = BUILT[key]
    solved = 0
    for _, states, extra in scenarios(key, st, a):
        try:
            r = run(st, a, states, extra)
        except SolveError as exc:
            assert exc.reason in {"blocked", "dry_run", "cavitation_range"}
            continue
        solved += 1
        assert abs(r.mass_residual_kg_s) < 1e-6 and abs(r.energy_residual_w) < 0.1
        assert max(abs(x.pressure_residual_pa) for x in (*r.units, *r.branches)) < 1.0
        assert run(st, a, states, extra) == r
    assert solved >= 15


@pytest.mark.parametrize("key", PARALLEL)
def test_parallel_stop_closes_check_valve_and_loads_the_other(key):
    a, b = ids(key)
    normal, r = solve(key), solve(key, {a: UnitState(running=False)})
    stopped, left = r.units
    assert stopped.status == "stopped" and stopped.flow_m3_s == 0 and stopped.shaft_w == 0
    assert not stopped.check_valve_open and stopped.reverse_dp_pa > 0
    assert left.flow_m3_s > normal.units[1].flow_m3_s and left.shaft_w > normal.units[1].shaft_w
    assert left.npsh_margin < normal.units[1].npsh_margin
    assert r.flow_m3_s < normal.flow_m3_s and downstream(key, r).flow_m3_s < downstream(key, normal).flow_m3_s


def test_weak_parallel_pump_dead_heads_without_reverse_flow_or_invented_power():
    r = solve("cdu", {"P-8101A": UnitState(speed_ratio=0.7)})
    weak = r.units[0]
    assert weak.status == "dead_headed" and weak.flow_m3_s == 0 and weak.shaft_w is None and weak.reverse_dp_pa > 0
    closed = solve("cdu", {"P-8101A": UnitState(valve_opening=0.0)}).units[0]
    assert closed.status == "dead_headed" and closed.reverse_dp_pa is None


@pytest.mark.parametrize("key", SERIES)
def test_series_stop_needs_an_explicit_choice(key):
    one, _ = ids(key)
    with pytest.raises(SolveError) as e:
        solve(key, {one: UnitState(running=False)})
    assert e.value.reason == "input"
    with pytest.raises(SolveError) as e:
        solve(key, {one: UnitState(running=False, stopped=Stopped(mode="block"))})
    assert e.value.reason == "blocked"
    with pytest.raises(ValidationError):
        Stopped(mode="pass")
    with pytest.raises(ValidationError):
        Stopped(mode="block", resistance_pa_s2_m6=1.0)


def test_series_pass_and_bypass_differ_from_block_and_dry_run_is_refused():
    st, a = BUILT["vrds"]
    two = ids("vrds")[1]
    u = st.units[0]
    r_pa = 0.1 * st.demand.density_kg_m3 * GRAVITY_M_S2 * u.pump.rated_head_m / u.pump.rated_flow_m3_s**2
    for mode in ("pass", "bypass"):
        r = solve("vrds", {two: UnitState(running=False, stopped=Stopped(mode=mode, resistance_pa_s2_m6=r_pa))})
        assert r.units[1].status == mode and 0 < r.flow_m3_s < solve("vrds").flow_m3_s
        with pytest.raises(SolveError) as e:            # 1단이 서면 2단 흡입이 증기압 아래로 간다
            solve("vrds", {ids("vrds")[0]: UnitState(running=False, stopped=Stopped(mode=mode, resistance_pa_s2_m6=r_pa))})
        assert e.value.reason == "dry_run"


@pytest.mark.parametrize("key", PARALLEL)
def test_parallel_twin_faults_mirror(key):
    a, b = ids(key)
    x, y = solve(key, {a: UnitState(head_loss=0.1)}), solve(key, {b: UnitState(head_loss=0.1)})
    assert x.units[0].flow_m3_s == y.units[1].flow_m3_s and x.header_abs_pa == y.header_abs_pa


@pytest.mark.parametrize("key", SERIES)
def test_series_stage_wear_gives_same_flow_but_different_interstage(key):
    one, two = ids(key)
    x, y = solve(key, {one: UnitState(head_loss=0.1)}), solve(key, {two: UnitState(head_loss=0.1)})
    assert x.flow_m3_s == pytest.approx(y.flow_m3_s, rel=1e-9)
    assert x.units[0].discharge_abs_pa < y.units[0].discharge_abs_pa * 0.95


@pytest.mark.parametrize("key", SERIES)
def test_interstage_resistance_location_is_not_observable(key):
    """1단 토출밸브 저항과 같은 크기의 2단 흡입 막힘은 기존 두 압력 태그 사이에 있어 기존 관측이 같다 (D9: 구분 불가).
    2026-11-27 2단 스트레이너 차압 계측을 더한 뒤에는 이 계측으로 구분된다."""
    st, a = BUILT[key]
    one, two = ids(key)
    v, rho = st.units[0].valve, st.demand.density_kg_m3
    added = v.resistance(rho, 0.1) - v.resistance(rho, 1.0)
    x = readings(a, solve(key, {one: UnitState(valve_opening=0.1)}))
    y = readings(a, solve(key, {two: UnitState(suction_resistance=added)}))
    strainer = {k for k in x if k.split(":")[1].startswith("strainer_")}
    assert {k: v for k, v in x.items() if k not in strainer} == pytest.approx({k: v for k, v in y.items() if k not in strainer}, rel=1e-9)
    dp = f"{two}:strainer_dp"
    assert y[dp] - x[dp] > 10 * sigma(a, dp)              # 스트레이너 차압이 막힘 위치를 가른다


def test_affinity_and_both_pumps_wear():
    st, a = BUILT["hou"]
    u = st.units[0]
    fast = solve("hou", {i: UnitState(speed_ratio=1.02) for i in ids("hou")})
    q = fast.units[0].flow_m3_s
    assert fast.units[0].head_m == pytest.approx(u.pump.shutoff_head_m * 1.02**2 - u.pump.curvature * q**2, rel=1e-12)


def test_common_suction_resistance_couples_both_pumps():
    r = solve("rfcc", common_suction_resistance=2e6)
    assert r.units[0].suction_abs_pa == r.units[1].suction_abs_pa
    assert BUILT["rfcc"][1].case.default_point.suction_abs_pa - r.units[0].suction_abs_pa == pytest.approx(2e6 * r.flow_m3_s**2, rel=1e-9)


def test_input_no_solution_and_boundary_errors():
    st, a = BUILT["hou"]
    point = a.case.default_point
    for states, reason in (({"X": UnitState()}, "input"),
                           ({i: UnitState(speed_ratio=0.2) for i in ids("hou")}, "reverse")):
        with pytest.raises(SolveError) as e:
            st.solve(point, states)
        assert e.value.reason == reason
    with pytest.raises(SolveError) as e:
        st.solve(point, common_suction_resistance=-1.0)
    assert e.value.reason == "input"
    with pytest.raises(SolveError) as e:
        st.solve(OperatingPoint(suction_abs_pa=point.suction_abs_pa, speed_rpm=point.speed_rpm, valve_openings={"virtual:none": 0.5}))
    assert e.value.reason == "input"
    with pytest.raises(SolveError) as e:
        st.solve(point, {ids("hou")[0]: UnitState(suction_resistance=1e10)})
    assert e.value.reason in {"dry_run", "cavitation_range", "reverse"}
    for bad in ({"speed_ratio": 0}, {"valve_opening": 1.1}, {"head_loss": 1.0}, {"suction_resistance": math.nan},
                {"stopped": Stopped(mode="block")}):
        with pytest.raises(ValidationError):
            UnitState(**bad)


def test_runout_is_refused():
    """하류 저항이 없으면 유량이 양정 곡선 끝에 닿는다."""
    st, a = BUILT["cdu"]
    branches = [b.model_copy(update={"outlet_abs_pa": 1000.0, "pipes": {k: p.model_copy(update={"diameter_m": 3.0}) for k, p in b.pipes.items()}})
                for b in st.demand.branches if b.booster is None]
    wide = st.model_copy(update={"demand": st.demand.model_copy(update={"branches": branches})})
    with pytest.raises(SolveError) as e:
        wide.solve(a.case.default_point)
    assert e.value.reason == "runout"


def test_valve_k_kv_cv_are_the_same_relation():
    v = BUILT["cdu"][0].units[0].valve
    assert v.kv(0.5) == pytest.approx(v.kv(1.0) / 2)
    assert v.kv(1.0) / v.cv(1.0) == pytest.approx(0.865, rel=1e-3)
    q = v.kv(1.0) / 3600
    assert v.pressure_loss_pa(q, 1000.0, 1.0) == pytest.approx(1e5)


def test_default_configuration_uses_the_station():
    """D5a: 새 펌프는 마스터 설비이고 정지 고장은 없다."""
    for key, (_, new) in UNITS.items():
        a = BUILT[key][1]
        assert new in a.plant.equipment_by_id() and a.roles[new] == "source"
