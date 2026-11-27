"""PU-D4·D5a: 두 펌프 구성 5곳(병렬 CDU·RFCC, 직렬 HOU·RHDS·VRDS)의 정상·고장 운전점을 계산해 남긴다. 제원은 config/ 의 station 이다.

실행: python -m scripts.verify_pump_network --out <새 폴더>
- normal.csv: 정상 해와 마스터 μ·한계. cases.csv: 고장·운전 변화. similarity.csv: 비슷한 고장의 관측 차이.
- summary.json: 설계 제원, 수지 잔차, 가정.
모두 synthetic assumption 이며 실제 설비의 직렬·병렬 관계가 아니다.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

from scripts.twin_expectations import PLANTS, breach
from scripts.verify_circuits import open_plant
from simulator.physics.pump import GRAVITY_M_S2
from simulator.physics.station import SolveError, Station, StationResult, Stopped, UnitState
from simulator.physics_adapter import PhysicsAdapter, booster_strainer_centers, drive_centers, strainer_centers

BUILT = {k: open_plant(k)[3] for k in PLANTS}
PARALLEL = [k for k, a in BUILT.items() if a.case.station.topology == "parallel"]
SERIES = [k for k in BUILT if k not in PARALLEL]
UNITS = {k: (a.primary, a.second) for k, a in BUILT.items()}


def build(key: str) -> tuple[Station, PhysicsAdapter]:
    """설정 YAML 의 station (D5a 부터 기본 구성)."""
    return BUILT[key].case.supply(), BUILT[key]


def readings(a: PhysicsAdapter, r: StationResult) -> dict[str, float]:
    """관측 태그 값('설비:신호'). 전류는 축동력 비례다. 체절 운전(dead_headed) 전류는 고정 효율 모델로 계산할 수 없어 NaN 이다."""
    old = a.solve()
    product = next(b for b in r.branches if b.branch_id == a.case.downstream_branch)
    p0 = next(b for b in old.branches if b.branch_id == a.case.downstream_branch)
    dst, out = a.keys[a.downstream], {}
    opening = a.case.default_point.valve_openings.get(a.case.control_valve, 1.0)   # 이 검증은 회로 제어밸브를 바꾸지 않는다
    for u, u0 in zip(r.units, old.units):
        src = a.keys[u.unit_id]
        out[f"{u.unit_id}:suction_pressure"] = a._gauge(src["suction_pressure"], u.suction_abs_pa)
        out[f"{u.unit_id}:discharge_pressure"] = a._gauge(src["discharge_pressure"], u.discharge_abs_pa)
        out[f"{u.unit_id}:flow"] = u.flow_m3_s * 3600
        out[f"{u.unit_id}:current"] = math.nan if u.shaft_w is None else a.control[src["current"]][0] * u.shaft_w / u0.shaft_w
        out.update({f"{u.unit_id}:{sig}": v for sig, v in _named(src, strainer_centers(a, src, u)).items()})
        out.update({f"{u.unit_id}:{sig}": v for sig, v in _named(src, drive_centers(src, u.speed_rpm, opening)).items()})
    out[f"{a.downstream}:flow"] = product.flow_m3_s * 3600
    out[f"{a.downstream}:discharge_pressure"] = a._gauge(dst["discharge_pressure"], product.booster_discharge_abs_pa)
    if "suction_pressure" in dst:
        out[f"{a.downstream}:suction_pressure"] = a._gauge(dst["suction_pressure"], product.booster_suction_abs_pa)
    if "current" in dst:
        out[f"{a.downstream}:current"] = a.control[dst["current"]][0] * product.booster_shaft_w / p0.booster_shaft_w
    out.update({f"{a.downstream}:{sig}": v for sig, v in _named(dst, booster_strainer_centers(a, dst, product)).items()})
    out.update({f"{a.downstream}:{sig}": v for sig, v in _named(dst, drive_centers(dst, product.booster_speed_rpm, opening)).items()})
    for eid, keys in a.keys.items():          # 헤더 합계 유량 = 입력 펌프 유량 합 (2026-11-27)
        if "header_flow" in keys:
            out[f"{eid}:header_flow"] = r.flow_m3_s * 3600
    for name in list(out):                    # 이중화 토출 압력 전송기 = 토출 압력 (2026-11-27)
        eid, sig = name.split(":")
        if sig == "discharge_pressure" and "redundant_discharge_pressure" in a.keys[eid]:
            out[f"{eid}:redundant_discharge_pressure"] = out[name]
    return out


def _named(keys: dict, centers: dict) -> dict[str, float]:
    """태그 키 → 신호 이름 (스트레이너 계측, 2026-11-27)."""
    return {sig: centers[k] for sig, k in keys.items() if k in centers}


def sigma(a: PhysicsAdapter, name: str) -> float:
    eid, signal = name.split(":")
    return a.control[a.keys[eid][signal]][1]


def normal_rows(key: str) -> list[dict]:
    st, a = build(key)
    rows = []
    for name, v in readings(a, st.solve(a.case.default_point)).items():
        eid, signal = name.split(":")
        tag_key = a.keys[eid][signal]
        limits = a.plant.tag_by_key()[tag_key].limits
        mean, sd = a.control[tag_key]
        rows.append({"plant": key, "tag": name, "tag_id": tag_key[1], "master_mean": mean, "solved": v, "delta_sigma": (v - mean) / sd,
                     "limit_hit": breach(v, limits.model_dump(exclude_none=True) if limits else {}) or ""})
    return rows


def stage_stop(st: Station, fraction: float, mode: str) -> Stopped:
    """정지한 직렬 펌프 저항의 민감도 값: 정격 유량에서 단 정격 양정의 fraction. 근거가 없어 기본값이 아니다."""
    u = st.units[0]
    return Stopped(mode=mode, resistance_pa_s2_m6=fraction * st.demand.density_kg_m3 * GRAVITY_M_S2 * u.pump.rated_head_m / u.pump.rated_flow_m3_s**2)


def scenarios(key: str, st: Station, a: PhysicsAdapter) -> list[tuple[str, dict, dict]]:
    """(이름, 펌프 상태, solve 추가 인자). 크기는 기존 회로의 최대 고장 크기를 쓴다."""
    case = a.case
    strainers = (case.max_strainer_resistance_pa_s2_m6, case.station.max_second_strainer_resistance_pa_s2_m6)
    out: list[tuple[str, dict, dict]] = [("normal", {}, {})]
    for i, u in enumerate(st.units, 1):
        x = u.unit_id
        out += [(f"wear{i}", {x: UnitState(head_loss=case.max_impeller_head_loss, efficiency_loss=case.max_impeller_efficiency_loss)}, {}),
                (f"clog{i}", {x: UnitState(suction_resistance=strainers[i - 1])}, {}),
                (f"valve{i}_10pct", {x: UnitState(valve_opening=0.1)}, {}),
                (f"valve{i}_closed", {x: UnitState(valve_opening=0.0)}, {}),
                (f"speed{i}_0.95", {x: UnitState(speed_ratio=0.95)}, {}),
                (f"speed{i}_0.85", {x: UnitState(speed_ratio=0.85)}, {}),
                (f"speed{i}_1.05", {x: UnitState(speed_ratio=1.05)}, {})]
        if st.topology == "parallel":
            out.append((f"stop{i}", {x: UnitState(running=False)}, {}))
        else:
            out.append((f"stop{i}_block", {x: UnitState(running=False, stopped=Stopped(mode="block"))}, {}))
            for mode in ("pass", "bypass"):
                for f in (0.02, 0.1, 0.3):
                    out.append((f"stop{i}_{mode}_{f}", {x: UnitState(running=False, stopped=stage_stop(st, f, mode))}, {}))
    both = {u.unit_id: UnitState(speed_ratio=0.95) for u in st.units}
    out += [("speed_both_0.95", both, {}),
            ("clog_common", {}, {"common_suction_resistance": case.max_strainer_resistance_pa_s2_m6 * (0.25 if st.topology == "parallel" else 1.0)}),
            ("product_valve_restricted", {}, {"restriction": 1.0}),
            ("booster_clog", {}, {"booster_suction_resistance": case.max_booster_strainer_resistance_pa_s2_m6})]
    return out


def run(st: Station, a: PhysicsAdapter, states: dict, extra: dict) -> StationResult:
    extra = dict(extra)
    _, point, _, _ = a.case.scenario(restriction=extra.pop("restriction", 0.0))
    return st.solve(point, states, **extra)


def case_rows(key: str) -> list[dict]:
    st, a = build(key)
    normal = st.solve(a.case.default_point)
    base, rows = readings(a, normal), []
    for name, states, extra in scenarios(key, st, a):
        row = {"plant": key, "topology": st.topology, "scenario": name, "status": "ok", "detail": ""}
        try:
            r = run(st, a, states, extra)
        except SolveError as exc:
            rows.append({**row, "status": exc.reason, "detail": str(exc)})
            continue
        got = readings(a, r)
        product = next(b for b in r.branches if b.branch_id == a.case.downstream_branch)
        row.update(total_flow_m3h=r.flow_m3_s * 3600, header_bar_abs=r.header_abs_pa / 1e5,
                   downstream_flow_m3h=product.flow_m3_s * 3600, downstream_npsh_margin=product.booster_npsh_margin,
                   mass_residual_kg_s=r.mass_residual_kg_s, energy_residual_w=r.energy_residual_w,
                   pressure_residual_pa=max([abs(u.pressure_residual_pa) for u in r.units] + [abs(b.pressure_residual_pa) for b in r.branches]))
        for i, u in enumerate(r.units, 1):
            row.update({f"u{i}_status": u.status, f"u{i}_flow_m3h": u.flow_m3_s * 3600, f"u{i}_rated_ratio": u.rated_flow_ratio,
                        f"u{i}_suction": got[f"{u.unit_id}:suction_pressure"], f"u{i}_discharge": got[f"{u.unit_id}:discharge_pressure"],
                        f"u{i}_current": got[f"{u.unit_id}:current"], f"u{i}_npsh_margin": u.npsh_margin,
                        f"u{i}_cavitation": u.cavitation_head_loss, f"u{i}_reverse_dp_bar": None if u.reverse_dp_pa is None else u.reverse_dp_pa / 1e5})
        row["sigma"] = json.dumps({k: None if math.isnan(v) else round((v - base[k]) / sigma(a, k), 1) for k, v in got.items()}, ensure_ascii=False)
        rows.append(row)
    return rows


def similarity_rows(key: str) -> list[dict]:
    """한 대 마모·막힘을 다른 대의 같은 고장(크기 자유)으로 가장 잘 흉내 냈을 때 남는 차이(σ). 잡음·모델 오차는 넣지 않는다."""
    st, a = build(key)
    normal = st.solve(a.case.default_point)
    base = readings(a, normal)
    case, ids = a.case, [u.unit_id for u in st.units]
    strainers = dict(zip(ids, (case.max_strainer_resistance_pa_s2_m6, case.station.max_second_strainer_resistance_pa_s2_m6)))

    def state(which: str, kind: str, x: float) -> UnitState:
        if kind == "wear":
            return UnitState(head_loss=x * case.max_impeller_head_loss, efficiency_loss=x * case.max_impeller_efficiency_loss)
        return UnitState(suction_resistance=x * strainers[which])

    def vector(which: str, kind: str, x: float) -> dict[str, float]:
        got = readings(a, st.solve(case.default_point, {which: state(which, kind, x)}))
        return {k: (v - base[k]) / sigma(a, k) for k, v in got.items()}

    # 관측 범위: 새 펌프 태그까지 / 기존 태그만 / 후속 펌프만
    rows, grid = [], [i / 20 for i in range(21)]
    downstream = [k for k in base if k.startswith(a.downstream)]
    for kind in ("wear", "clog"):
        for truth, other in (ids, ids[::-1]):
            library = [vector(other, kind, x) for x in grid]
            for x in (0.3, 0.6, 1.0):
                t = vector(truth, kind, x)
                for scope, keys in (("all_tags", list(base)), ("existing_tags", [k for k in base if k.split(":")[0] != ids[1]]),
                                    ("downstream_only", downstream)):
                    best = min(range(len(grid)), key=lambda i: sum((t[k] - library[i][k])**2 for k in keys))
                    gap = {k: t[k] - library[best][k] for k in keys}
                    top = max(gap, key=lambda k: abs(gap[k]))
                    rows.append({"plant": key, "topology": st.topology, "fault": kind, "true_unit": truth, "confused_with": other, "severity": x,
                                 "scope": scope, "best_other_severity": grid[best], "residual_norm_sigma": math.sqrt(sum(g**2 for g in gap.values())),
                                 "largest_tag": top, "largest_gap_sigma": gap[top], "truth_norm_sigma": math.sqrt(sum(t[k]**2 for k in keys))})
    return rows


def write(path: Path, rows: list[dict]) -> None:
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", type=Path, required=True, help="새로 만들 폴더")
    args = p.parse_args()
    args.out.mkdir(parents=True)
    write(args.out / "normal.csv", [r for k in UNITS for r in normal_rows(k)])
    write(args.out / "cases.csv", [r for k in UNITS for r in case_rows(k)])
    write(args.out / "similarity.csv", [r for k in UNITS for r in similarity_rows(k)])
    design = {k: build(k)[0].model_dump() for k in UNITS}
    (args.out / "design.json").write_text(json.dumps(design, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
