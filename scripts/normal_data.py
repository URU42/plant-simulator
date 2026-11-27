"""AE 학습·보정용 정상 데이터(합성)를 재현 가능하게 만들고 한계 이탈을 센다.

실행: python -m scripts.normal_data --out <새 폴더> [--train-hours 72] [--test-hours 3] [--plants cdu hou ...] [--twin] [--no-quiet]
- <out>/train·test/<공장>_<설비>.csv: timestamp, 태그…, label, scenario_id, event_id (sk-clx `app.ae_pca` 입력 형식, 전부 정상 label 0).
  10초 태그는 10초마다 측정하고 사이 행은 직전 값으로 채운다(그 사이 행은 학습이 읽지 않는다).
- <out>/normal-values.csv: 공장·설비·태그·측정 주기. manifest.json: 구성 버전·시드·기간·파일 지문·태그별 한계 이탈 수.
기본은 일반 모드 실행 설정 그대로(조용한 정상, 물리 회로 포함)이고 사건을 넣지 않는다. 같은 인자면 파일 지문이 같다.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

from common.config_models import load_plant, load_runtime
from common.sim_clock import KST
from scripts.twin_expectations import PLANTS, breach
from simulator.model import PlantGenerator

CONFIG = Path(__file__).resolve().parents[1] / "config"
START = datetime(2026, 11, 12, tzinfo=KST)
CHUNK = timedelta(hours=1)


def write_plant(key: str, out: Path, train_hours: float, test_hours: float, variant: str, quiet: bool) -> dict:
    plant = load_plant(CONFIG / f"plant_{key}.yaml")
    runtime = load_runtime(CONFIG / f"runtime_{key}{variant}.yaml").model_copy(update={"quiet_normal": quiet})
    generator = PlantGenerator(plant, runtime)
    limits = {k: t.limits.model_dump(exclude_none=True) if t.limits else {} for k, t in plant.tag_by_key().items()}
    control = plant.control_by_key()
    stats = {k: [0, 0.0, 0.0, 0] for k in control}            # 표본 수, z 합, z 제곱합, 한계 이탈 수
    files: dict[str, str] = {}
    at = START
    for split, hours in (("train", train_hours), ("test", test_hours)):      # test 는 train 바로 뒤 구간
        (out / split).mkdir(exist_ok=True)
        end = at + timedelta(hours=hours)
        handles = {e.equip_id: (out / split / f"{key}_{e.equip_id}.csv").open("x", newline="", encoding="utf-8") for e in plant.equipment}
        writers = {e: csv.writer(h, lineterminator="\n") for e, h in handles.items()}
        for e in plant.equipment:
            writers[e.equip_id].writerow(["timestamp", *(t.tag_id for t in e.tags), "label", "scenario_id", "event_id"])
        last: dict[tuple[str, str], float] = {}
        while at < end:
            stop = min(at + CHUNK, end)
            rows: dict[datetime, dict[tuple[str, str], float]] = {}
            for s in generator.generate(at, stop):
                k = (s.equip_id, s.tag_id)
                rows.setdefault(s.ts, {})[k] = s.value
                z = (s.value - control[k][0]) / control[k][1]
                st = stats[k]
                st[0] += 1
                st[1] += z
                st[2] += z * z
                st[3] += breach(s.value, limits[k]) is not None
            for ts in sorted(rows):
                last.update(rows[ts])
                for e in plant.equipment:
                    writers[e.equip_id].writerow([ts.isoformat(), *(last[e.equip_id, t.tag_id] for t in e.tags), 0, "", ""])
            at = stop
        for e, h in handles.items():
            h.close()
            with (out / split / f"{key}_{e}.csv").open("rb") as f:
                files[f"{split}/{key}_{e}.csv"] = hashlib.file_digest(f, "sha256").hexdigest()
    return {"plant_id": plant.plant.plant_id, "config_version": plant.plant.config_version, "seed": runtime.seed, "runtime": f"runtime_{key}{variant}.yaml",
            "quiet_normal": quiet, "start": START.isoformat(), "end": at.isoformat(), "files": files,
            "limit_hits": {"/".join(k): v[3] for k, v in stats.items() if v[3]}, "samples": sum(v[0] for v in stats.values()),
            "tags": {"/".join(k): {"samples": v[0], "z_mean": v[1] / v[0], "z_std": (v[2] / v[0] - (v[1] / v[0])**2)**0.5} for k, v in stats.items()}}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", type=Path, required=True, help="새로 만들 폴더")
    p.add_argument("--train-hours", type=float, default=72)
    p.add_argument("--test-hours", type=float, default=3)
    p.add_argument("--plants", nargs="+", choices=PLANTS, default=list(PLANTS))
    p.add_argument("--twin", action="store_true", help="물리 회로 실행 설정(runtime_<공장>_twin.yaml)")
    p.add_argument("--no-quiet", action="store_true", help="억제 표집을 끈 정상 (우연한 Nelson 위반 포함)")
    a = p.parse_args()
    a.out.mkdir(parents=True)
    with (a.out / "normal-values.csv").open("x", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["plant", "equip_id", "tag_id", "interval_sec"])
        for key in a.plants:
            runtime = load_runtime(CONFIG / f"runtime_{key}.yaml")
            for e in load_plant(CONFIG / f"plant_{key}.yaml").equipment:
                w.writerows([key, e.equip_id, t.tag_id, t.interval_sec or runtime.sample_interval_sec] for t in e.tags)
    result = {key: write_plant(key, a.out, a.train_hours, a.test_hours, "_twin" if a.twin else "", not a.no_quiet) for key in a.plants}
    (a.out / "manifest.json").write_text(json.dumps({"provenance": "synthetic assumption", "plants": result}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for key, r in result.items():
        print(key, r["samples"], "limit_hits:", r["limit_hits"])


if __name__ == "__main__":
    main()
