"""CDU 물리 모델의 오프라인 재생 데이터 생성."""
from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from common.config_models import PlantConfig, RuntimeConfig, load_plant, load_runtime, load_yaml
from common.message import Sample, SCHEMA_VERSION
from common.sim_clock import KST
from simulator.fault import FaultEvent, load_fault_modes, plan, tag_effect
from simulator.instrument import InstrumentEvent
from simulator.physics import KGF_CM2_PA, PumpCircuit

SIGNAL_UNITS = {
    "suction_pressure": "kg/cm2(g)",
    "discharge_pressure": "kg/cm2(g)",
    "flow": "m3/h",
    "current": "A",
}
START = datetime(2026, 11, 24, tzinfo=KST)


def replay(
    circuit: PumpCircuit,
    plant: PlantConfig,
    runtime: RuntimeConfig,
    start: datetime,
    duration_sec: int,
    delay_sec: int,
    fault: FaultEvent | None = None,
    dropouts: Iterable[InstrumentEvent] = (),
) -> tuple[dict, dict]:
    if start.utcoffset() is None or start.microsecond or duration_sec <= 0 or delay_sec < 0:
        raise ValueError("시간대가 있는 정초 시작 시각, 양의 재생 길이, 음이 아닌 지연이 필요합니다.")
    start = start.astimezone(KST)
    equipment = plant.equipment_by_id()[circuit.equip_id]
    if (plant.plant.plant_id != circuit.plant_id or equipment.unit_id != circuit.unit_id
            or not any(source == circuit.equip_id and target == circuit.outlet_node and stream == "process"
                       for source, target, _, stream in plant.flow.edges)):
        raise ValueError("물리 모델의 공장·Unit·공정 연결이 설비 설정과 일치해야 합니다.")
    candidates = [tag for tag in equipment.tags if tag.signal in SIGNAL_UNITS]
    tags = {tag.signal: tag for tag in candidates}
    if len(candidates) != len(SIGNAL_UNITS) or {k: t.unit for k, t in tags.items()} != SIGNAL_UNITS:
        raise ValueError("지원하는 각 신호에 기존 단위의 태그가 하나씩 필요합니다.")
    suction = tags["suction_pressure"]
    current = tags["current"]
    derive = current.derive
    if derive is None:
        raise ValueError("전류 태그에 기존 파생식이 필요합니다.")
    reference = {tag.tag_id: tag.control.mean for tag in tags.values()}
    inputs = [reference[tag_id] for tag_id in derive.inputs]
    control = plant.control_by_key()
    suction_key = (equipment.equip_id, suction.tag_id)
    suction_mean, suction_sigma = control[suction_key]
    effect = None
    if fault:
        if fault.equip_id != equipment.equip_id:
            raise ValueError("고장 주입 대상이 물리 모델의 설비와 일치해야 합니다.")
        effect = plan(plant, fault.mode, fault.equip_id, control).get(suction_key)
        if effect is None:
            raise ValueError("흡입 압력 효과가 있는 고장만 물리 모델에 주입할 수 있습니다.")

    dropouts = tuple(event for event in dropouts if event.kind == "dropout" and event.equip_id == equipment.equip_id)
    rng = np.random.default_rng(runtime.seed)
    frames = [{"at": (start + timedelta(seconds=offset)).isoformat(), "samples": []}
              for offset in range(duration_sec + delay_sec)]
    states = []
    for offset in range(duration_sec):
        ts = start + timedelta(seconds=offset)
        epoch = int(ts.timestamp())
        z = 0.0
        if fault and effect:
            z, _ = tag_effect(effect, epoch - fault.start,
                              None if fault.stop is None else fault.stop - fault.start,
                              fault.speed, None if fault.repair is None else fault.repair - fault.start)
        state = circuit.solve(circuit.atmospheric_pa + (suction_mean + suction_sigma * z) * KGF_CM2_PA)
        states.append({"ts": ts.isoformat(), **state.__dict__})
        values = {
            suction.tag_id: (state.suction_abs_pa - circuit.atmospheric_pa) / KGF_CM2_PA,
            tags["discharge_pressure"].tag_id: (state.discharge_abs_pa - circuit.atmospheric_pa) / KGF_CM2_PA,
            tags["flow"].tag_id: state.flow_m3_s * 3600,
        }
        values[current.tag_id] = derive.value([values[tag_id] for tag_id in derive.inputs], inputs,
                                              current.control.mean)
        for tag in tags.values():
            if epoch % (tag.interval_sec or runtime.sample_interval_sec):
                continue
            sigma = derive.noise_sigma if tag is current else control[(equipment.equip_id, tag.tag_id)][1]
            measured = round(values[tag.tag_id] + float(rng.normal(0, sigma)), tag.decimals)
            if any(e.start <= epoch < e.end and tag.tag_id in e.tag_ids for e in dropouts):
                continue
            sample = Sample(equip_id=equipment.equip_id, tag_id=tag.tag_id, ts=ts, value=measured)
            frames[offset + delay_sec]["samples"].append(sample.model_dump(mode="json"))

    observation = {
        "mode": "mock", "sample_schema_version": SCHEMA_VERSION, "plant_id": circuit.plant_id,
        "unit_id": circuit.unit_id, "equip_id": circuit.equip_id, "outlet_node": circuit.outlet_node,
        "tags": [{"tag_id": tag.tag_id, "unit": tag.unit, "interval_sec": tag.interval_sec or runtime.sample_interval_sec}
                 for tag in tags.values()],
        "frames": frames,
    }
    truth = {
        "configuration": circuit.model_dump(), "states": states,
        "injection": None if fault is None else {
            "event_id": fault.event_id, "fault_mode": fault.mode.id, "equip_id": fault.equip_id,
            "start_epoch_sec": fault.start, "stop_epoch_sec": fault.stop, "repair_epoch_sec": fault.repair,
            "speed": fault.speed,
        },
        "dropouts": [event.__dict__ for event in dropouts],
    }
    return observation, truth


def write_replay(output: Path, observation: dict, truth: dict) -> None:
    output.mkdir(mode=0o700)
    public = output / "public"
    private = output / "private"
    public.mkdir()
    private.mkdir(mode=0o700)
    for path, data in ((private / "truth.json", truth), (public / "measurements.json", observation)):
        with path.open("x", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, allow_nan=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="새로 만들 출력 디렉터리")
    parser.add_argument("--normal", action="store_true", help="흡입 압력 고장 주입 없이 재생")
    args = parser.parse_args()
    config = Path("config")
    circuit = PumpCircuit.model_validate(load_yaml(config / "physics_cdu.yaml"))
    plant = load_plant(config / "plant_cdu.yaml")
    runtime = load_runtime(config / "runtime_cdu.yaml")
    start = int(START.timestamp())
    mode = load_fault_modes(config / "fault_modes.yaml")["PM-SUCTION-LOSS"]
    fault = None if args.normal else FaultEvent("CDU-DEMO-01", circuit.equip_id, mode, start + 15, start + 55, 20)
    equipment = plant.equipment_by_id()[circuit.equip_id]
    scripted = InstrumentEvent("CDU-DEMO-DROPOUT", "dropout", circuit.equip_id,
                               tuple(tag.tag_id for tag in equipment.tags), start + 40, start + 44)
    data = replay(circuit, plant, runtime, START, 75, 2, fault, (scripted,))
    write_replay(args.output, *data)


if __name__ == "__main__":
    main()
