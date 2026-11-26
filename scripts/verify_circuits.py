"""D3: 다섯 공장 대표 결합 회로를 생성기로 재현해 회로 범위와 정상·약함·심화·회복·수리 결과를 남긴다.

실행: python -m scripts.verify_circuits --out <새 폴더>
- scope.json: 설정 파일에서 읽은 지원 노드·계산 태그·경계 조건·묶은 노드·그래프에만 있는 간선·미지원 설비.
- cases.csv: (공장, 주입 설비, 모드, 단계, 시드) × 태그의 창 평균, 정상 대비 변화(σ), 자기 한계 판정.
- summary.json: 설비별 판정(자기 태그·한계만)과 후속 변화 유무, 회로 밖 태그 불변 여부, 수지 잔차.
값은 모두 합성 등가회로의 결과이며 현장 검증이 아니다.
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timedelta
from pathlib import Path

from collector.anomaly_book import AnomalyBook
from collector.seed_master import seed_master
from collector.storage import Storage
from common.config_models import PlantConfig, TagKey, load_plant, load_runtime
from common.sim_clock import KST
from scripts.twin_expectations import PLANTS, breach
from simulator.fault import FaultEvent, load_fault_modes
from simulator.model import NoEvents, PlantGenerator
from simulator.physics_adapter import PhysicsAdapter, load_physics

CONFIG = Path(__file__).resolve().parents[1] / "config"
START = datetime(2026, 11, 12, 1, tzinfo=KST)
EPOCH = int(START.timestamp())
MODES = load_fault_modes(CONFIG / "fault_modes.yaml")
WINDOW = 30
CHANGED = 2.5                   # 후속 설비가 변했다고 보는 크기(σ). 화면의 연결 설비 영향 기준(IMPACT_SIGMA)과 같다
# 모드 → (약함 시각, 심화 시각). 해제는 심화 + 60초, 수리는 해제 + 420초. 약함은 최대의 약 10 % 크기가 되는 시각이다.
TIMES = {"PM-SUCTION-LOSS": (133, 1600), "PM-IMPELLER-WEAR": (30, 400),
         "PM-DISCHARGE-VALVE-RESTRICTION": (30, 400), "OP-THROUGHPUT-CHANGE": (150, 400)}
EQUATIONS = {
    "pump": "H(Q,N) = (N/N0)^2*H0 - a*Q^2, a = (H0 - Hr)/Qr^2 (simulator/physics/pump.py)",
    "pipe": "dp = rho*(f*L/D + K)/(2*A^2)*Q^2 (simulator/physics/pipe.py)",
    "valve": "K(opening) = K_open/opening^2 (simulator/physics/valve.py)",
    "suction_strainer": "dp = R*Q^2, R = blockage * max_resistance",
    "station": "병렬: 공통 흡입·헤더, Q = Q1 + Q2. 직렬: Q1 = Q2, p_s2 = p_d1 - R_line*Q^2 (simulator/physics/station.py)",
    "header": "Q_station = sum(Q_branch), 헤더 압력은 이분법 (simulator/physics/station.py)",
    "branch": "p_header + rho*g*H_booster = p_outlet + rho*g*dz + (R_branch + R_strainer + R_tail)*Q^2",
    "npsh": "H' = H*(1 - d(NPSHa/NPSHr)), eta' = eta*(1 - e*d) (simulator/physics/npsh.py)",
    "current": "I = I_normal * P_shaft/P_shaft_normal (축동력 비례 대용)",
}


def open_plant(key: str) -> tuple[PlantConfig, object, object, PhysicsAdapter]:
    plant = load_plant(CONFIG / f"plant_{key}.yaml")
    runtime = load_runtime(CONFIG / f"runtime_{key}_twin.yaml").model_copy(update={"quiet_normal": False})
    physics = load_physics(plant, runtime.physics)
    return plant, runtime, physics, next(a for a in physics.adapters if isinstance(a, PhysicsAdapter))


def scope(key: str) -> dict:
    plant, _, physics, a = open_plant(key)
    case, path = a.case, a.case.downstream_path
    inside = set(zip(path, path[1:]))
    label = {(x, y): side for x, y, side, _ in plant.flow.edges}
    inside |= {(a.second, path[1])} if a.case.station.topology == "parallel" else set()
    near = [list(e) for e in plant.flow.edges if (e[0] in path or e[1] in path) and (e[0], e[1]) not in inside]
    solved = a.solve(0.0, 0.0, 0.0)
    return {
        "plant": plant.plant.plant_id, "source": a.primary, "second": a.second, "topology": a.case.station.topology, "downstream": a.downstream, "provenance": case.provenance,
        "computed_equipment": sorted(a.roles), "computed_tags": sorted("/".join(k) for k in a.tag_keys),
        "lumped_path": [{"from": x, "to": y, "side": label[(x, y)]} for x, y in zip(path, path[1:])],
        "lumped_nodes_not_computed": [n for n in dict.fromkeys(path[1:-1]) if n != a.second],
        "graph_only_edges": near, "returns_to_path_not_closed": [e for e in near if e[0] in a.roles],
        "boundaries": {"suction_abs_pa": case.default_point.suction_abs_pa, "speed_rpm": case.default_point.speed_rpm,
                       "density_kg_m3": case.network.density_kg_m3, "atmospheric_pa": case.atmospheric_pa,
                       "outlets": {b.branch_id: {"node": b.outlet_node, "abs_pa": b.outlet_abs_pa, "elevation_m": b.elevation_change_m}
                                   for b in case.network.branches}},
        "virtual_elements": [i for b in case.network.branches for i in (*b.pipes, *b.valves)],
        "equations": EQUATIONS,
        "max_parameters": {k: v for k, v in case.model_dump().items() if k.startswith("max_")},
        "unsupported_equipment": physics.exclusions, "unsupported_scenarios": physics.scenario_exclusions,
        "normal_solution": {"header_abs_pa": solved.header_abs_pa, "flow_m3_h": solved.total_flow_m3_s * 3600,
                            "branch_flow_m3_h": {b.branch_id: b.flow_m3_s * 3600 for b in solved.branches}},
    }


def judge(plant: PlantConfig, equip_id: str, means: dict[TagKey, float]) -> tuple[str, dict[str, str]]:
    """설비 판정: 그 설비 자신의 태그 값과 자신의 한계만 본다 (다른 설비 값·주입 정보 없음)."""
    hits = {t.tag_id: b for t in plant.equipment_by_id()[equip_id].tags
            if (equip_id, t.tag_id) in means and (b := breach(means[equip_id, t.tag_id], t.limits.model_dump(exclude_none=True) if t.limits else {}))}
    return next((lv for lv in ("trip", "alarm", "warn") if lv in hits.values()), "normal"), hits


def window_means(gen: PlantGenerator, at: int) -> dict[TagKey, float]:
    sums: dict[TagKey, list[float]] = {}
    for s in gen.generate(START + timedelta(seconds=at - WINDOW), START + timedelta(seconds=at)):
        sums.setdefault((s.equip_id, s.tag_id), []).append(s.value)
    return {k: sum(v) / len(v) for k, v in sums.items()}


def phases(mode_id: str) -> dict[str, int]:
    weak, severe = TIMES[mode_id]
    out = {"normal": 600, "weak": 600 + weak + WINDOW // 2, "severe": 600 + severe, "released": 600 + severe + 60 + 400}
    if any(s.damage for s in MODES[mode_id].stages):
        out["repaired"] = 600 + severe + 60 + 420 + 400
    return out


def run_case(key: str, equip_id: str, mode_id: str, seed: int) -> dict:
    """한 사례: 같은 시드의 정상 실행과 짝지은 단계별 창 평균."""
    plant, runtime, physics, a = open_plant(key)
    runtime = runtime.model_copy(update={"seed": seed})
    control, inject = plant.control_by_key(), EPOCH + 600
    stop = inject + TIMES[mode_id][1] + 60
    storage = Storage(":memory:")
    try:
        seed_master(storage, plant)
        book = AnomalyBook(storage, plant, MODES, physics=physics)
        book.inject(equip_id, mode_id, inject)
        book.stop(equip_id, stop)
        if any(s.damage for s in MODES[mode_id].stages):
            book.repair(equip_id, stop + 420)
        events: list[FaultEvent] = book.external(EPOCH, EPOCH + 10_000)
        own = {k for k, tp in book._plan(MODES[mode_id], equip_id).items() if tp.via != "physics"}
    finally:
        storage.close()

    class Events:
        def external(self, start, end):
            return events

    plans, rows = physics.parameter_plans(events), []
    for phase, at in phases(mode_id).items():
        state = physics.state(EPOCH + at - WINDOW // 2, plans)[physics.adapters.index(a)]
        base = window_means(PlantGenerator(plant, runtime, NoEvents()), at)
        got = window_means(PlantGenerator(plant, runtime, Events()), at)
        delta = {k: (got[k] - base[k]) / control[k][1] for k in got}
        judged = {e: judge(plant, e, got) for e in a.roles}
        linked = {"/".join(k): round(delta[k], 2) for k in sorted(a.tag_keys) if k[0] != equip_id}
        rows.append({
            "phase": phase, "mean": {"/".join(k): got[k] for k in sorted(a.tag_keys)}, "base": {"/".join(k): base[k] for k in sorted(a.tag_keys)},
            "delta_sigma": {"/".join(k): delta[k] for k in sorted(a.tag_keys)},
            "judgement": {e: {"level": lv, "tags": hits} for e, (lv, hits) in judged.items()},
            "linked_changed": [k for k, v in linked.items() if abs(v) >= CHANGED], "linked_unchanged": [k for k, v in linked.items() if abs(v) < CHANGED],
            "outside_changed": sorted("/".join(k) for k in got if got[k] != base[k] and k not in a.tag_keys and k not in own),
            "residuals": {"mass_kg_s": state.mass_residual_kg_s, "pump_pa": state.pump_pressure_residual_pa,
                          "branch_pa": max(abs(b.pressure_residual_pa) for b in state.branches)},
        })
    return {"plant": key, "equip_id": equip_id, "mode": mode_id, "code": MODES[mode_id].label.code, "seed": seed, "phases": rows}


def cases() -> list[tuple[str, str, str]]:
    return [(key, e, m) for key in PLANTS for _, _, physics, a in [open_plant(key)] for e in a.roles for m in physics.scenarios[e]]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", type=Path, required=True, help="새로 만들 폴더")
    p.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    args = p.parse_args()
    args.out.mkdir(parents=True)
    results = [run_case(*c, seed) for c in cases() for seed in args.seeds]
    (args.out / "scope.json").write_text(json.dumps({k: scope(k) for k in PLANTS}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (args.out / "summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    with (args.out / "cases.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["plant", "injected", "mode", "code", "seed", "phase", "tag", "normal", "value", "delta_sigma", "own_limit"])
        for r in results:
            for ph in r["phases"]:
                for tag, v in ph["mean"].items():
                    e, t = tag.split("/")
                    w.writerow([r["plant"], r["equip_id"], r["mode"], r["code"], r["seed"], ph["phase"], tag, round(ph["base"][tag], 4),
                                round(v, 4), round(ph["delta_sigma"][tag], 2), ph["judgement"][e]["tags"].get(t, "")])
    for r in results:
        if r["seed"] == args.seeds[0]:
            for ph in r["phases"]:
                lv = {e: j["level"] for e, j in ph["judgement"].items()}
                print(r["plant"], r["equip_id"], r["mode"], ph["phase"], lv, "linked:", len(ph["linked_changed"]), "outside:", ph["outside_changed"])


if __name__ == "__main__":
    main()
