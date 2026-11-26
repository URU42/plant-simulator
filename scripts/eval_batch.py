"""평가 배치: 같은 운전 조건·시드의 쌍대 센서 시계열과, 따로 보관하는 정답지를 만든다.

실행: python -m scripts.eval_batch --out <관측 폴더> --eval-dir <정답 폴더> [--train 1 --val 2 --test 3] [--cases 이름 ...]
- 쌍(group)은 공장·실행 설정·시드·시간 창이 같고 처리(arm)만 다르다. 억제 표집을 꺼서 두 arm 의 정상 잡음이 같다.
- 분할은 시드 단위다. 한 쌍·한 사건·한 잡음 실현이 두 분할에 나뉘지 않는다.
- 관측 폴더: v3 측정값(equip_id, tag_id, ts, value)과 실행 목록만. 결측은 행이 없다(채우지 않는다).
- 정답 폴더(0700): 실행 → 쌍·arm·시나리오, 사건별 주입·첫 반응·첫 관측·해제·수리·복귀 시각.
크기·시간은 모두 synthetic 이다. 물리 회로 사례(twin)도 합성 시뮬레이션이다.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from collector.anomaly_book import AnomalyBook
from collector.seed_master import seed_master
from collector.storage import Storage
from common.config_models import PlantConfig, RuntimeConfig, TagKey, load_plant, load_runtime
from common.message import SCHEMA_VERSION, Sample
from common.sim_clock import KST
from simulator.fault import FaultMode, load_fault_modes
from simulator.instrument import InstrumentEvent
from simulator.model import EventSource, NoEvents, PlantGenerator

CONFIG = Path(__file__).resolve().parents[1] / "config"
DAY = datetime(2026, 11, 12, tzinfo=KST)
FIELDS = ("equip_id", "tag_id", "ts", "value")
EPS = 1e-6                  # 잡음 없는 효과가 0 이 아니라고 보는 크기 (σ 배수)
LEAD, JITTER = 180, 300     # 주입은 시작 뒤 LEAD ~ LEAD + JITTER 초
WINDOW = 2700               # 모든 실행의 길이 (초). 길이로 시나리오를 알 수 없게 하나로 둔다


@dataclass(frozen=True)
class Case:
    """쌍 하나의 설계. mode 는 고장 모드, instrument 는 계측 이상 종류, off 는 대조 arm 에서 끌 교호작용 규칙."""

    name: str
    scenario: str           # normal_stable / normal_load / equipment_fault / instrument / interaction
    plant: str
    equip: str = ""
    mode: str = ""
    hold: int = 0           # 주입 → 해제 (초)
    speed: int = 1
    repair: bool = False
    instrument: str = ""
    tag: str = ""           # 계측 이상을 걸 태그 (dropout 은 설비 전체)
    off: str = ""
    twin: bool = False      # 물리 회로 실행 설정


CASES = (
    Case("stable-rhds", "normal_stable", "rhds"),
    Case("stable-cdu-twin", "normal_stable", "cdu", twin=True),
    Case("load-change", "normal_load", "rhds", "M-3101", "OP-LOAD-CHANGE", 600),
    Case("throughput-change", "normal_load", "rhds", "P-3101", "OP-THROUGHPUT-CHANGE", 600),
    Case("low-flow", "normal_load", "rfcc", "C-1401", "OP-LOW-FLOW", 600),
    Case("surge", "equipment_fault", "rhds", "C-3101", "CC-SURGE", 400),
    Case("gear-repaired", "equipment_fault", "rhds", "GB-3101", "GB-LUBE-WEAR", 500, 5, True),
    Case("gear-damaged", "equipment_fault", "rhds", "GB-3101", "GB-LUBE-WEAR", 500, 5),
    Case("pump-wear-twin", "equipment_fault", "cdu", "P-8101A", "PM-IMPELLER-WEAR", 300, 10, True, twin=True),
    Case("fault-dropout", "equipment_fault", "rhds", "C-3101", "CC-SURGE", 400, instrument="dropout"),
    Case("spike", "instrument", "rhds", "C-3101", instrument="spike", tag="FI-C3101"),
    Case("stuck-temp", "instrument", "rhds", "GB-3101", instrument="stuck", tag="TI-B-GB3101"),
    Case("dropout", "instrument", "rhds", "P-3102", instrument="dropout"),
    Case("stuck-twin", "instrument", "cdu", "P-8101A", instrument="stuck", tag="FI-8101A", twin=True),
    Case("i1", "interaction", "rhds", "P-3101", "OP-THROUGHPUT-CHANGE", 600, off="I1"),
    Case("i2", "interaction", "rfcc", "C-1401", "CC-SURGE-HIGH-RATIO", 400, off="I2"),
    Case("i3", "interaction", "rhds", "P-3102", "PM-SUCTION-HOT", 600, 5, off="I3"),
    Case("i4", "interaction", "rhds", "F-3101", "FN-FOULING-SPEED-UP", 600, 5, True, off="I4"),
    Case("i5", "interaction", "rfcc", "C-1401", "CC-SURGE-HIGH-RATIO", 400, off="I5"),
    Case("i6", "interaction", "rhds", "GB-3101", "GB-LUBE-WEAR", 500, 5, True, off="I6"),
)


class _Fixed:
    """지정한 계측 이상 일정 (InstrumentSchedule 과 같은 events)."""

    def __init__(self, items: list[InstrumentEvent]) -> None:
        self.items = items

    def events(self, start: float, end: float) -> list[InstrumentEvent]:
        return [e for e in self.items if e.start < end and e.end > start]


def _id(*parts) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16]


def _iso(epoch: float | None) -> str | None:
    return None if epoch is None else datetime.fromtimestamp(epoch, KST).isoformat()


def _fine(plant: PlantConfig) -> PlantConfig:
    """모든 태그를 1초·소수 12자리로 본 구성안. 두 arm 의 차이가 잡음 없는 공정 효과가 된다."""
    return plant.model_copy(update={"equipment": [e.model_copy(update={"tags": [
        t.model_copy(update={"interval_sec": None, "decimals": 12}) for t in e.tags]}) for e in plant.equipment]})


def _by_tag(samples: list[Sample]) -> dict[TagKey, dict[int, float]]:
    out: dict[TagKey, dict[int, float]] = {}
    for s in samples:
        out.setdefault((s.equip_id, s.tag_id), {})[int(s.ts.timestamp())] = s.value
    return out


def _effect_tags(process, process0, sent, sent0, sigma, at: float, end: int) -> list[dict]:
    """공정 효과가 있는 태그마다 첫 반응·첫 관측·복귀 시각. 관측은 실제로 보낸 표본만 본다(누락은 관측이 아니다)."""
    rows = []
    for key, base in process0.items():
        moved = [ep for ep, v in base.items() if abs(process[key][ep] - v) > EPS * sigma[key]]
        if not moved:
            continue
        seen = [ep for ep, v in sent.get(key, {}).items() if v != sent0[key][ep]]
        restored = max(moved) + 1 if max(moved) + 1 < end else None
        rows.append({"equip_id": key[0], "tag_id": key[1], "first_response_at": moved[0], "first_observed_at": min(seen, default=None),
                     "restored_at": restored, "response_delay_sec": moved[0] - at})
    return rows


def _instrument_event(ev: InstrumentEvent, sent, clean) -> dict:
    """계측 이상 사건의 시각. 공정은 반응하지 않으므로 first_response_at 은 없다. 관측은 깨끗한 측정과 다른(또는 빠진) 첫 시각."""
    rows = []
    for tag_id in ev.tag_ids:
        key = (ev.equip_id, tag_id)
        wrong = [ep for ep, v in clean[key].items() if ev.start <= ep < ev.end and sent.get(key, {}).get(ep) != v]
        missing = [ep for ep in wrong if ep not in sent.get(key, {})]
        after = min((ep for ep in clean[key] if ep >= ev.end), default=None)
        rows.append({"equip_id": ev.equip_id, "tag_id": tag_id, "first_response_at": None, "first_observed_at": min(wrong, default=None),
                     "restored_at": after if wrong else None, "response_delay_sec": None, "missing": len(missing)})
    return {"event_id": ev.event_id, "kind": ev.kind, "code": 2, "equip_id": ev.equip_id, "injected_at": ev.start,
            "released_at": ev.end, "repaired_at": None, "damage_remains": False, "tags": rows}


def _summarize(event: dict) -> dict:
    """사건 단위 시각(태그의 최소·최대)을 더하고 시각을 KST ISO 로 바꾼다. 한 태그라도 안 돌아오면 복귀는 없다(null)."""
    tags = event["tags"]
    first = lambda k: min((t[k] for t in tags if t[k] is not None), default=None)
    ends = [t["restored_at"] for t in tags]
    event |= {"first_response_at": first("first_response_at"), "first_observed_at": first("first_observed_at"),
              "restored_at": max(ends) if ends and None not in ends else None}
    stop, repair = event["released_at"], event["repaired_at"]
    for t in tags:      # 회복은 그 태그를 되돌린 해제(손상이 남았던 태그는 수리)부터. 해제 전에 스스로 끝난 효과는 0
        end = t["restored_at"]
        t["recovery_sec"] = None if end is None else max(0, end - (repair if repair is not None and end > repair else stop))
    for row in (event, *tags):
        for k in row:
            if k.endswith("_at"):
                row[k] = _iso(row[k])
    return event


def build_pair(case: Case, seed: int, modes: dict[str, FaultMode]) -> tuple[dict, list[tuple[str, list[Sample], dict]]]:
    """쌍 하나: (쌍 정보, [(arm, 보낸 표본, 정답)]). 표본·정답 모두 (case, seed) 로만 정해진다."""
    plant = load_plant(CONFIG / f"plant_{case.plant}.yaml")
    runtime: RuntimeConfig = load_runtime(CONFIG / f"runtime_{case.plant}{'_twin' if case.twin else ''}.yaml")
    runtime = runtime.model_copy(update={"seed": seed, "quiet_normal": False})
    rng = np.random.default_rng([seed, int(_id(case.name), 16) % 2**32])
    start = int((DAY + timedelta(minutes=int(rng.integers(0, 12 * 60)))).timestamp())
    inject = start + LEAD + int(rng.integers(0, JITTER))
    stop = inject + case.hold
    repair = stop + 420 if case.repair else None        # 수리 전 손상만 남은 구간(온도 회복 300초 뒤)을 둔다
    end = start + WINDOW
    t0, t1 = (datetime.fromtimestamp(x, KST) for x in (start, end))
    sigma = {k: s for k, (_, s) in plant.control_by_key().items()}

    def book(p: PlantConfig) -> tuple[EventSource, list[dict]]:
        physics = PlantGenerator(p, runtime).physics
        storage = Storage(":memory:")
        try:
            seed_master(storage, p)
            b = AnomalyBook(storage, p, modes, physics=physics)
            b.inject(case.equip, case.mode, inject, case.speed)
            b.stop(case.equip, stop)
            if repair is not None and not b.repair(case.equip, repair)[1]:
                raise ValueError(f"{case.name}: no damage to repair")
            return b, b.answer_key(None, None, None)
        finally:
            storage.close()

    instrument = []
    if case.instrument:
        tags = tuple(t.tag_id for t in plant.equipment_by_id()[case.equip].tags) if case.instrument == "dropout" else (case.tag,)
        step = max((t.interval_sec or 1) for t in plant.equipment_by_id()[case.equip].tags if t.tag_id in tags)
        at = -(-inject // step) * step
        dur = {"spike": step, "stuck": 180, "dropout": 60, "bias": 600, "drift": 600}[case.instrument]
        size = {"spike": (6, 10), "bias": (4, 6), "drift": (6, 10)}.get(case.instrument)   # σ 배수 (synthetic 가정)
        z = float(rng.uniform(*size)) * (1 if rng.random() < 0.5 else -1) if size and case.instrument != "spike" \
            else float(rng.uniform(*size)) if size else 0.0
        instrument.append(InstrumentEvent(f"IM-{_id(case.name, seed)}", case.instrument, case.equip, tags, at, at + dur, z))

    def generate(p: PlantConfig, source: EventSource, off: frozenset[str], faults=()) -> list[Sample]:
        g = PlantGenerator(p, runtime, source, off)
        g.instrument = _Fixed(list(faults)) if faults else None
        return g.generate(t0, t1)

    fine = _fine(plant)
    source, key = book(plant) if case.mode else (NoEvents(), [])
    null = frozenset()
    if case.off:
        arms = [("on", source, null, []), ("off", source, frozenset({case.off}), [])]
    else:
        arms = [("reference", NoEvents(), null, []), ("treated", source, null, instrument)]
    out, sents = [], []
    for arm, src, off, faults in arms:
        samples = generate(plant, src, off, faults)
        sent = _by_tag(samples)
        sents.append(sent)
        events = []
        if not isinstance(src, NoEvents):
            reference = _by_tag(generate(plant, NoEvents(), off))
            rows = _effect_tags(_by_tag(generate(fine, src, off)), _by_tag(generate(fine, NoEvents(), off)), sent, reference, sigma, inject, end)
            k = key[0]
            events.append({"event_id": f"{k['event_id']}-{_id(case.name, seed)}", "kind": k["kind"], "code": k["code"],
                           "mode": case.mode, "cause": k["cause"], "equip_id": case.equip, "speed": case.speed, "injected_at": inject,
                           "released_at": stop, "repaired_at": repair, "damage_remains": k["damage_remains"] and repair is None, "tags": rows})
        if faults:
            clean = _by_tag(generate(plant, src, off))
            events += [_instrument_event(ev, sent, clean) for ev in faults]
        out.append((arm, samples, {"arm": arm, "interactions_off": sorted(off), "events": [_summarize(e) for e in events]}))
    changed = sorted(f"{k[0]}/{k[1]}" for k in sents[0].keys() | sents[1].keys() if sents[0].get(k) != sents[1].get(k))
    info = {"group_id": _id("group", case.name, seed), "case": case.name, "scenario": case.scenario, "plant_id": plant.plant.plant_id,
            "seed": seed, "runtime": "twin" if case.twin else "base", "quiet_normal": False, "start_at": _iso(start), "end_at": _iso(end),
            "interaction": case.off or None, "changed_tags": changed}
    return info, out


def _write_gz(path: Path, samples: list[Sample]) -> str:
    """보낸 표본을 그대로 쓴다. gzip 머리의 시각·이름을 비워 같은 입력이면 파일 바이트도 같다."""
    text = io.StringIO()
    w = csv.writer(text, lineterminator="\n")
    w.writerow(FIELDS)
    w.writerows((s.equip_id, s.tag_id, s.ts.isoformat(), s.value) for s in samples)
    data = gzip.compress(text.getvalue().encode(), mtime=0)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, data) -> None:
    path.touch(mode=0o600)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, allow_nan=False) + "\n", encoding="utf-8")


def write_batch(out: Path, eval_dir: Path, splits: dict[str, list[int]], cases: tuple[Case, ...] = CASES) -> dict:
    out, eval_dir = out.resolve(), eval_dir.resolve()
    if out == eval_dir or out in eval_dir.parents or eval_dir in out.parents:
        raise ValueError("observations and answers must be in separate directories")
    seeds = [s for v in splits.values() for s in v]
    if len(seeds) != len(set(seeds)):
        raise ValueError("a seed may belong to one split only (leak)")
    out.mkdir(parents=True)
    eval_dir.mkdir(parents=True, mode=0o700)
    eval_dir.chmod(0o700)
    modes = load_fault_modes(CONFIG / "fault_modes.yaml")
    manifest, truth = [], []
    for split, ss in splits.items():
        if ss:
            (out / split).mkdir()
        for seed in ss:
            for case in cases:
                info, arms = build_pair(case, seed, modes)
                for arm, samples, answer in arms:
                    run_id = _id("run", case.name, seed, arm)
                    file = f"{split}/{run_id}.csv.gz"
                    digest = _write_gz(out / file, samples)
                    manifest.append({"run_id": run_id, "split": split, "plant_id": info["plant_id"], "file": file,
                                     "start_at": info["start_at"], "end_at": info["end_at"], "rows": len(samples), "sha256": digest})
                    truth.append({"run_id": run_id, "split": split} | info | answer)
    manifest.sort(key=lambda r: r["run_id"])         # 생성 순서가 시나리오·쌍을 드러내지 않게
    _write_json(out / "manifest.json", {"schema_version": SCHEMA_VERSION, "fields": FIELDS, "runs": manifest})
    _write_json(eval_dir / "truth.json", {"provenance": "synthetic assumption", "splits": splits, "runs": truth})
    return {"runs": len(manifest), "groups": len({t["group_id"] for t in truth}), "rows": sum(r["rows"] for r in manifest)}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", type=Path, required=True, help="새로 만들 관측 폴더")
    p.add_argument("--eval-dir", type=Path, required=True, help="새로 만들 정답 폴더 (관측 폴더 밖)")
    for name, seed in (("train", 1), ("val", 2), ("test", 3)):
        p.add_argument(f"--{name}", type=int, nargs="*", default=[seed], help=f"{name} 시드")
    p.add_argument("--cases", nargs="+", choices=[c.name for c in CASES], help="만들 사례 (기본: 전부)")
    a = p.parse_args()
    cases = tuple(c for c in CASES if not a.cases or c.name in a.cases)
    print(json.dumps(write_batch(a.out, a.eval_dir, {"train": a.train, "val": a.val, "test": a.test}, cases)))


if __name__ == "__main__":
    main()
