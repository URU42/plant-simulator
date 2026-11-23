import pytest

from common.config_models import load_yaml
from models.modelica.verify import CoupledCase, comparison_row, models, read_final_row


def test_modelica_inputs_and_python_operating_points_share_yaml(config_dir):
    case = CoupledCase.model_validate(load_yaml(config_dir / "physics_cdu_integrated.yaml"))
    source, points = models(case)
    assert len(points) == 5
    assert "source(p=375990)" in source
    assert "strainer(resistance=14000000)" in source
    assert points["SpeedLow"][0].speed_rpm == 3240
    assert points["SpeedHigh"][0].speed_rpm == pytest.approx(3960)
    assert points["ValveRestricted"][0].valve_openings[case.control_valve] == 0.4
    assert points["StrainerBlocked"][1] == case.max_strainer_resistance_pa_s2_m6
    flow = {k: case.network.solve(*args).pump_flow_m3_s for k, args in points.items()}
    assert flow["SpeedLow"] < flow["Normal"] < flow["SpeedHigh"]
    assert flow["ValveRestricted"] < flow["Normal"]
    assert flow["StrainerBlocked"] < flow["Normal"]


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
