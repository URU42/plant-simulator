"""교호작용 I1~I6 의 2×2 × on/off 같은 시드 대조 실험과 모델 종류 커버리지를 CSV 로 쓴다.

실행: python -m scripts.interaction_experiment --out <폴더> [--seeds 1 2 3]
대조 실험은 기본 실행 설정(일반 모드), 커버리지는 기본·트윈 실행 설정 둘 다 본다.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from common.config_models import load_plant, load_runtime
from simulator.fault import load_fault_modes
from simulator.interactions import MEASURE, RULES, coverage, experiment, normal_low_flow, run_cell, targets

CONFIG = Path(__file__).resolve().parents[1] / "config"
PLANTS = ("cdu", "hou", "rfcc", "rhds", "vrds")


def _write(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    modes = load_fault_modes(CONFIG / "fault_modes.yaml")
    plants = {k: load_plant(CONFIG / f"plant_{k}.yaml") for k in PLANTS}
    cover = [{"runtime": v or "base", "plant_id": plants[k].plant.plant_id} | r for k in PLANTS for v in ("", "twin")
             for r in coverage(plants[k], load_runtime(CONFIG / f"runtime_{k}{'_' + v if v else ''}.yaml"), modes)]
    _write(args.out / "coverage.csv", cover)
    rows = []
    for k, plant in plants.items():
        rt = load_runtime(CONFIG / f"runtime_{k}.yaml")
        for rule in RULES:
            for eid in targets(rule, plant, modes):
                for seed in args.seeds:
                    rows += experiment(rule, plant, rt, eid, modes, seed)
    _write(args.out / "contrast.csv", rows)
    # I5: 같은 저유량의 정상 운전과 합성 서지
    rt, pair = load_runtime(CONFIG / "runtime_rfcc.yaml"), []
    for seed in args.seeds:
        normal = normal_low_flow(plants["rfcc"], rt, "C-1401", modes, seed)
        for case, (high, label) in {"surge_low_flow": ((True, False), "HL"), "surge_low_flow_high_ratio": ((True, True), "HH")}.items():
            surge = run_cell("I5", plants["rfcc"], rt, "C-1401", modes, high, True, seed)
            pair += [{"seed": seed, "tag_id": t, "case": case, "cell": label, "normal_mean": normal[t].mean(), "normal_std": normal[t].std(),
                      "surge_mean": surge[t].mean(), "surge_std": surge[t].std()}
                     for t in ("FI-C-1401", "PD-C-1401", "VX-C-1401", "VY-C-1401")]
    _write(args.out / "i5_normal_vs_surge.csv", pair)
    print(f"cells={len(rows)} rows, window={MEASURE}, out={args.out}")


if __name__ == "__main__":
    main()
