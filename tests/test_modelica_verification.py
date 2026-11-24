import pytest

from common.config_models import load_yaml
from models.modelica.verify import CoupledCase, comparison_row, models, read_final_row


def test_modelica_inputs_and_python_operating_points_share_yaml(config_dir):
    case = CoupledCase.model_validate(load_yaml(config_dir / "physics_cdu_integrated.yaml"))
    source, points = models(case)
    assert len(points) == 9
    assert "source(p=375990)" in source
    assert "strainer(resistance=14000000)" in source
    assert "boosterStrainer(resistance=1074440000)" in source
    assert points["SpeedLow"][1].speed_rpm == 3240
    assert points["SpeedHigh"][1].speed_rpm == pytest.approx(3960)
    assert points["ValveRestricted"][1].valve_openings[case.control_valve] == 0.4
    assert points["StrainerBlocked"][2] == case.max_strainer_resistance_pa_s2_m6
    assert points["BoosterStrainerBlocked"][3] == case.max_booster_strainer_resistance_pa_s2_m6
    assert points["ThroughputReduced"][1].valve_openings[case.control_valve] == pytest.approx(0.88)
    assert points["ImpellerWorn"][0].pump.efficiency == pytest.approx(0.6)
    flow = {k: network.solve(point, r, rb).pump_flow_m3_s for k, (network, point, r, rb) in points.items()}
    assert flow["SpeedLow"] < flow["Normal"] < flow["SpeedHigh"]
    assert flow["ValveRestricted"] < flow["Normal"]
    assert flow["StrainerBlocked"] < flow["Normal"]
    assert flow["ImpellerWorn"] < flow["Normal"]
    assert flow["ValveRestricted"] < flow["ThroughputReduced"] < flow["Normal"]
    assert flow["BoosterStrainerBlocked"] < flow["Normal"]


@pytest.mark.parametrize("key", ["cdu", "hou", "rfcc", "rhds", "vrds"])
def test_every_plant_circuit_builds_all_modelica_scenarios(config_dir, key):
    case = CoupledCase.model_validate(load_yaml(config_dir / f"physics_{key}_integrated.yaml"))
    source, points = models(case)
    assert source.count("extends PlantHydraulics.Circuit(") == len(points) == 9
    for network, point, r, rb in points.values():
        result = network.solve(point, r, rb)
        assert abs(result.mass_residual_kg_s) < 1e-9
        assert all(b.pressure_residual_pa == pytest.approx(0, abs=1e-4) for b in result.branches)


def test_balance_requires_both_models_to_close():
    common_bias = comparison_row("Normal", "massResidual", 1e-5, 1e-5)
    assert common_bias["absolute_error"] == 0 and common_bias["relative_error"] is None
    assert not common_bias["passed"]
    pressure = comparison_row("Normal", "headerPressure", 2_000_000, 2_000_001.5)
    assert pressure["passed"] and pressure["tolerance"] == 2.0
    assert pressure["relative_error"] == pytest.approx(7.5e-7)


def test_incomplete_or_invalid_omc_result_is_rejected(tmp_path):
    file = tmp_path / "result.csv"
    file.write_text("time,feedFlow\n0,0.1\n0.5,0.1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="종료 시각"):
        read_final_row(file)
    with pytest.raises(ValueError, match="유한한"):
        comparison_row("Normal", "feedFlow", 0.1, float("nan"))


@pytest.mark.parametrize("name", ["physics_cdu_p8303a.yaml", "physics_cdu_f8201.yaml"])
def test_single_circuit_modelica_inputs_cover_cavitation_regimes(config_dir, name):
    from models.modelica.verify import single_models, single_outputs
    from simulator.physics_single import SingleCase
    case = SingleCase.model_validate(load_yaml(config_dir / name))
    source, specs = single_models(case)
    assert source.count("extends PlantHydraulics.SingleCircuit(") == len(specs) == (9 if case.npsh else 6)
    assert ("cavitation=true" in source) == (case.npsh is not None)
    losses = [single_outputs(*spec).get("headLoss", 0.0) for spec in specs.values()]
    if case.npsh is not None:
        # 캐비테이션 없음, 시작 구간(x ≤ 1), 급경사 구간(x > 1)을 모두 비교한다.
        assert min(losses) == 0 and any(0 < x <= 0.03 for x in losses) and max(losses) > 0.1
    for spec in specs.values():
        out = single_outputs(*spec)
        assert abs(out["pressureResidual"]) < 1e-6 and abs(out["energyResidual"]) < 1e-3
