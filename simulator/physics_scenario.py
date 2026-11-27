"""기존 생성기로 막힘·회복 및 마모·수리 예제의 계측 CSV와 평가용 JSON을 만든다."""
import argparse
import csv
import json
from datetime import datetime, timedelta
from pathlib import Path

from collector.anomaly_book import AnomalyBook
from collector.seed_master import seed_master
from collector.storage import Storage
from common.config_models import load_plant, load_runtime
from common.sim_clock import KST
from simulator.fault import load_fault_modes
from simulator.model import PlantGenerator

START = datetime(2026, 11, 24, tzinfo=KST)
SNAPSHOTS = (0, 40, 70, 80, 110, 140, 190, 210, 250, 280)


def write_example(output: Path, config: Path) -> None:
    plant = load_plant(config / "plant_cdu.yaml")
    runtime = load_runtime(config / "runtime_cdu.yaml").model_copy(update={
        "physics": config / "physics_cdu_integrated.yaml",
        "units": ["UNIT-CDU-01", "UNIT-CDU-07"],
    })
    generator = PlantGenerator(plant, runtime)
    physics = generator.physics
    assert physics is not None
    modes = load_fault_modes(config / "fault_modes.yaml")
    storage = Storage(":memory:")
    try:
        seed_master(storage, plant)
        book = AnomalyBook(storage, plant, modes, physics=physics)
        generator.source = book
        start = int(START.timestamp())
        end = START + timedelta(seconds=SNAPSHOTS[-1] + 1)
        book.inject(physics.primary, "PM-SUCTION-LOSS", start + 10, speed=10)
        book.stop(physics.primary, start + 80)
        book.inject(physics.primary, "PM-IMPELLER-WEAR", start + 160, speed=10)
        book.stop(physics.primary, start + 200)
        book.repair(physics.primary, start + 220)
        samples = generator.generate(START, end)
        events = physics.parameter_plans(book.external(start, end.timestamp()))
        instants = [START + timedelta(seconds=offset) for offset in SNAPSHOTS]
        evaluation = {
            "provenance": "synthetic assumption", "seed": runtime.seed,
            "events": book.answer_key(None, None, None),
            "states": [{"ts": ts.isoformat(), **physics.state(int(ts.timestamp()), events).model_dump(exclude_none=True)}
                       for ts in instants],
        }
    finally:
        storage.close()

    output.mkdir(mode=0o700)
    (output / "observations").mkdir()
    (output / "evaluation").mkdir(mode=0o700)
    selected = set(instants)
    with (output / "observations" / "measurements.csv").open("x", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, ["equip_id", "tag_id", "ts", "value"], lineterminator="\n")
        writer.writeheader()
        writer.writerows(s.model_dump(mode="json") for s in samples if s.ts in selected)
    with (output / "evaluation" / "truth.json").open("x", encoding="utf-8") as file:
        json.dump(evaluation, file, ensure_ascii=False, indent=2, allow_nan=False)
        file.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="새로 만들 출력 경로")
    parser.add_argument("--config-dir", type=Path, default=Path("config"))
    args = parser.parse_args()
    write_example(args.output, args.config_dir)


if __name__ == "__main__":
    main()
