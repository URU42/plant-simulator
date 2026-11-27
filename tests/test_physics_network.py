import json
import subprocess
import sys
from math import pi

import pytest
from pydantic import ValidationError

from common.config_models import load_plant, load_yaml
from simulator.physics.__main__ import CaseResult, NetworkCase
from simulator.physics.network import Network, OperatingPoint
from simulator.physics.pipe import Pipe
from simulator.physics.pump import GRAVITY_M_S2
from simulator.physics.valve import Valve


@pytest.fixture
def case(config_dir):
    return NetworkCase.model_validate(load_yaml(config_dir / "physics_cdu_network.yaml"))


def test_branch_mass_and_pressure_balance(case):
    network = case.network
    point = case.default_point
    result = network.solve(point)
    density = network.density_kg_m3
    pump = network.pump
    total = sum(branch.flow_m3_s for branch in result.branches)
    assert all(branch.flow_m3_s > 0 for branch in result.branches)
    assert density * result.pump_flow_m3_s == pytest.approx(density * total, abs=1e-9, rel=0)
    assert abs(result.mass_residual_kg_s) < 1e-9

    speed_ratio = point.speed_rpm / pump.reference_speed_rpm
    expected_head = speed_ratio**2 * pump.shutoff_head_m - (
        pump.shutoff_head_m - pump.rated_head_m) * (total / pump.rated_flow_m3_s)**2
    expected_discharge = point.suction_abs_pa + density * GRAVITY_M_S2 * expected_head
    assert result.discharge_abs_pa == pytest.approx(expected_discharge, abs=1e-7, rel=0)
    assert abs(result.pump_pressure_residual_pa) < 1e-7
    assert result.hydraulic_w == pytest.approx(total * (result.discharge_abs_pa - point.suction_abs_pa))

    for branch, solved in zip(network.branches, result.branches):
        losses = 0.0
        for pipe in branch.pipes.values():
            velocity = solved.flow_m3_s / (pi * pipe.diameter_m**2 / 4)
            losses += density * velocity**2 / 2 * (pipe.darcy_factor * pipe.length_m / pipe.diameter_m
                                                   + pipe.minor_loss_k)
        for key, valve in branch.valves.items():
            area = pi * valve.diameter_m**2 / 4 * point.valve_openings[key]
            losses += density * valve.full_open_k * (solved.flow_m3_s / area)**2 / 2
        delta = branch.outlet_abs_pa + density * GRAVITY_M_S2 * branch.elevation_change_m + losses
        assert result.discharge_abs_pa == pytest.approx(delta, abs=1e-7, rel=0)
        assert abs(solved.pressure_residual_pa) < 1e-7


def test_valve_restriction_redistributes_flow(case):
    point = case.default_point
    normal = case.network.solve(point)
    restricted = case.network.solve(OperatingPoint(
        suction_abs_pa=point.suction_abs_pa, speed_rpm=point.speed_rpm,
        valve_openings={**point.valve_openings, "virtual:main-valve": 0.3},
    ))
    assert restricted.pump_flow_m3_s < normal.pump_flow_m3_s
    assert restricted.discharge_abs_pa > normal.discharge_abs_pa
    assert restricted.branches[0].flow_m3_s < normal.branches[0].flow_m3_s
    assert restricted.branches[1].flow_m3_s > normal.branches[1].flow_m3_s


def test_speed_affinity_and_network_response(case):
    point = case.default_point
    pump = case.network.pump
    speed = point.speed_rpm * 1.1
    assert pump.head_m(pump.rated_flow_m3_s * 1.1, speed) == pytest.approx(pump.rated_head_m * 1.1**2)
    fast = case.network.solve(OperatingPoint(**{**point.model_dump(), "speed_rpm": speed}))
    normal = case.network.solve(point)
    assert fast.pump_flow_m3_s > normal.pump_flow_m3_s
    assert fast.discharge_abs_pa > normal.discharge_abs_pa


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_invalid_flows_and_density(case, bad):
    pump = case.network.pump
    pipe = next(iter(case.network.branches[0].pipes.values()))
    valve = next(iter(case.network.branches[0].valves.values()))
    for calculate in (lambda: pump.head_m(bad, 3600), lambda: pipe.pressure_loss_pa(bad, 850),
                      lambda: valve.pressure_loss_pa(bad, 850, 1.0), lambda: pipe.resistance(bad),
                      lambda: valve.resistance(bad, 1.0)):
        with pytest.raises(ValueError):
            calculate()
    with pytest.raises(ValidationError):
        Network.model_validate({**case.network.model_dump(), "density_kg_m3": bad})


def test_invalid_parameters_and_unsupported_operating_points(case):
    with pytest.raises(ValidationError):
        Pipe(length_m=70, diameter_m=0, darcy_factor=0.025)
    with pytest.raises(ValidationError):
        Valve(diameter_m=0.25, full_open_k=-1)
    with pytest.raises(ValidationError):
        OperatingPoint(suction_abs_pa=375990, speed_rpm=3600, valve_openings={"virtual:main-valve": 0})
    with pytest.raises(ValueError, match="등록되지 않은 밸브"):
        case.network.solve(OperatingPoint(suction_abs_pa=375990, speed_rpm=3600,
                                          valve_openings={"virtual:unknown": 0.5}))
    with pytest.raises(ValueError, match="역류"):
        case.network.solve(OperatingPoint(suction_abs_pa=375990, speed_rpm=2000))


def test_example_scope_and_cli_json_contract(case, config_dir, tmp_path):
    plant = load_plant(config_dir / "plant_cdu.yaml")
    assert case.plant_id == plant.plant.plant_id
    equipment = plant.equipment_by_id()[case.equip_id]
    assert case.unit_id == equipment.unit_id
    assert any(a == case.equip_id and b == case.network.branches[0].outlet_node and stream == "process"
               for a, b, _, stream in plant.flow.edges)
    assert {tag.signal: tag.tag_id for tag in equipment.tags if tag.signal in {
        "suction_pressure", "discharge_pressure", "flow", "current",
    }} == {
        "suction_pressure": "PI-8101A", "discharge_pressure": "PI-8101B",
        "flow": "FI-8101A", "current": "II-8101A",
    }
    point = tmp_path / "point.json"
    point.write_text(json.dumps({"suction_abs_pa": 380000, "speed_rpm": 3650,
                                "valve_openings": {"virtual:main-valve": 0.9}}))
    command = [sys.executable, "-m", "simulator.physics", "--config",
               str(config_dir / "physics_cdu_network.yaml")]
    run = subprocess.run([*command, "--point", str(point)], check=True, capture_output=True)
    report = CaseResult.model_validate_json(run.stdout)
    assert report.equip_id == "P-8101A"
    assert report.result.speed_rpm == 3650
    assert report.result.suction_abs_pa == 380000
    schema = subprocess.run([*command, "--schema", "point"], check=True, capture_output=True)
    assert json.loads(schema.stdout)["required"] == ["suction_abs_pa", "speed_rpm"]
    point.write_text('{"suction_abs_pa": -1, "speed_rpm": 3600}')
    failed = subprocess.run([*command, "--point", str(point)], capture_output=True)
    assert failed.returncode == 2
    assert failed.stdout == b""
    assert b"suction_abs_pa" in failed.stderr
