import json
from datetime import timedelta
from math import pi

import pytest

from common.config_models import load_plant, load_runtime, load_yaml
from common.message import Sample
from simulator.fault import FaultEvent, load_fault_modes
from simulator.instrument import InstrumentEvent
from simulator.physics import KGF_CM2_PA, PumpCircuit
from simulator.physics_demo import START, replay, write_replay


@pytest.fixture
def cdu(config_dir):
    return (
        PumpCircuit.model_validate(load_yaml(config_dir / "physics_cdu.yaml")),
        load_plant(config_dir / "plant_cdu.yaml"),
        load_runtime(config_dir / "runtime_cdu.yaml"),
    )


@pytest.mark.parametrize("suction_gauge", [1.55, 2.02, 2.8008])
def test_pump_curve_pipe_and_balances(cdu, suction_gauge):
    circuit, _, _ = cdu
    state = circuit.solve(circuit.atmospheric_pa + suction_gauge * KGF_CM2_PA)
    q = state.flow_m3_s
    velocity = q / (pi * circuit.pipe_diameter_m**2 / 4)
    loss = (circuit.darcy_factor * circuit.pipe_length_m / circuit.pipe_diameter_m
            + circuit.minor_loss_coefficient) * circuit.density_kg_m3 * velocity**2 / 2
    assert state.discharge_abs_pa - state.outlet_abs_pa == pytest.approx(loss)
    assert state.inlet_mass_kg_s == pytest.approx(state.outlet_mass_kg_s, abs=1e-9)
    assert abs(state.mass_residual_kg_s) < 1e-9
    assert abs(state.energy_residual_w) < 1e-6
    assert state.outlet_temp_k > state.inlet_temp_k
    assert state.hydraulic_w == pytest.approx(state.shaft_w * circuit.efficiency)


def test_calibration_and_unsupported_operating_point(cdu):
    circuit, _, _ = cdu
    state = circuit.solve(2.8008 * KGF_CM2_PA + circuit.atmospheric_pa)
    assert state.flow_m3_s * 3600 == pytest.approx(401.34795)
    assert (state.discharge_abs_pa - circuit.atmospheric_pa) / KGF_CM2_PA == pytest.approx(27.814)
    with pytest.raises(ValueError, match="흡입 절대압"):
        circuit.solve(float("nan"))
    with pytest.raises(ValueError, match="양의 유량"):
        circuit.model_copy(update={"outlet_abs_pa": 4e6}).solve(state.suction_abs_pa)


def test_existing_fault_plan_changes_physical_operating_point(cdu, config_dir):
    mode = load_fault_modes(config_dir / "fault_modes.yaml")["PM-SUCTION-LOSS"]
    epoch = int(START.timestamp())
    event = FaultEvent("PRIVATE-ID", "P-8101A", mode, epoch + 1, epoch + 38, 20)
    observation, truth = replay(*cdu, START, 55, 0, event)
    states = truth["states"]
    assert states[0]["flow_m3_s"] == pytest.approx(states[1]["flow_m3_s"])
    assert states[31]["suction_abs_pa"] < states[0]["suction_abs_pa"]
    assert states[31]["flow_m3_s"] < states[0]["flow_m3_s"]
    assert states[-1]["flow_m3_s"] > states[38]["flow_m3_s"]
    assert max(abs(state["energy_residual_w"]) for state in states) < 1e-6
    assert truth["injection"]["event_id"] == "PRIVATE-ID"
    assert "PRIVATE-ID" not in json.dumps(observation)


def test_sensor_contract_delay_noise_and_dropout_are_independent_of_physics(cdu, tmp_path):
    epoch = int(START.timestamp())
    dropout = InstrumentEvent("PRIVATE-DROPOUT", "dropout", "P-8101A", ("FI-8101A",), epoch + 2, epoch + 4)
    regular, truth = replay(*cdu, START, 6, 0)
    delayed, delayed_truth = replay(*cdu, START, 6, 2, dropouts=[dropout])
    assert truth["states"] == delayed_truth["states"]
    assert delayed["frames"][:2] == [
        {"at": START.isoformat(), "samples": []},
        {"at": (START + timedelta(seconds=1)).isoformat(), "samples": []},
    ]
    assert regular == replay(*cdu, START, 6, 0)[0]
    tag_ids = {"PI-8101A", "PI-8101B", "FI-8101A", "II-8101A"}
    assert {tag["tag_id"] for tag in regular["tags"]} == tag_ids
    assert {tag["interval_sec"] for tag in regular["tags"]} == {1}
    for i, (expected, observed) in enumerate(zip(regular["frames"], delayed["frames"][2:])):
        retained = [sample for sample in expected["samples"]
                    if not (2 <= i < 4 and sample["tag_id"] == "FI-8101A")]
        assert observed["samples"] == retained
        for raw in observed["samples"]:
            sample = Sample.model_validate(raw)
            assert sample.ts == START + timedelta(seconds=i)
    flows = [sample["value"] for frame in regular["frames"] for sample in frame["samples"]
             if sample["tag_id"] == "FI-8101A"]
    assert len(set(flows)) > 1
    assert all(state["flow_m3_s"] == truth["states"][0]["flow_m3_s"] for state in truth["states"])

    destination = tmp_path / "replay"
    write_replay(destination, delayed, delayed_truth)
    assert [file.name for file in (destination / "public").iterdir()] == ["measurements.json"]
    public_blob = (destination / "public" / "measurements.json").read_text()
    assert "PRIVATE-DROPOUT" not in public_blob
    assert set(json.loads(public_blob)) == {
        "mode", "sample_schema_version", "plant_id", "unit_id", "equip_id", "outlet_node", "tags", "frames",
    }
    assert json.loads((destination / "private" / "truth.json").read_text())["dropouts"][0]["event_id"] == "PRIVATE-DROPOUT"
    with pytest.raises(FileExistsError):
        write_replay(destination, regular, truth)
