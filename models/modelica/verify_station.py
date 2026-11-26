"""직렬·병렬 펌프 묶음을 Modelica 로 따로 풀어 simulator.physics.station 과 비교한다 (PU-D4, D5a 부터 기본 구성의 회로).

실행: python -m models.modelica.verify_station --omc <omc> --output <새 폴더>
Python 은 헤더 이분법, Modelica 는 연립 비선형 방정식(시작값은 설계 정격 유량, Python 해를 넘기지 않음)이다.
고장 크기 시나리오 비교(models.modelica.verify)도 여기의 text·expected 를 쓴다.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
from pathlib import Path

from models.modelica.verify import HERE, assignment, pipe_args, pump_args, read_final_row, run_omc
from scripts.verify_pump_network import UNITS, build, scenarios
from simulator.physics.pump import GRAVITY_M_S2
from simulator.physics.station import SolveError

NAMES = ("normal", "wear1", "wear2", "clog1", "clog2", "valve1_10pct", "speed1_0.95", "speed2_0.85", "stop1", "stop2",
         "clog_common", "product_valve_restricted", "booster_clog", "stop1_pass_0.02", "stop2_pass_0.1", "stop2_bypass_0.3", "valve2_closed")


def text(name: str, case, st, point, states: dict, common_r: float = 0.0, booster_r: float = 0.0) -> str:
    """한 사례의 전체 모델. 닫힌 차단밸브는 그 펌프를 회로에서 뺀다(병렬). 모든 펌프가 운전 중이면 질량·기계 에너지 수지도 낸다."""
    net = st.demand
    product = next(b for b in net.branches if b.branch_id == case.downstream_branch)
    other = next(b for b in net.branches if b is not product)
    rho = net.density_kg_m3
    comp, conn = [], []

    def add(kind: str, mod: str) -> str:
        comp.append(f"    {kind} {mod};")
        return mod.split("(")[0]

    add("Boundary", assignment("source", p=point.suction_abs_pa))
    add("Boundary", assignment("otherSink", p=other.outlet_abs_pa))
    add("Boundary", assignment("productSink", p=product.outlet_abs_pa))
    add("Strainer", assignment("common", resistance=common_r))
    conn.append("connect(source.a, common.a);")
    fluid = lambda s: s[:-1] + f", rho={rho:.17g}, g={GRAVITY_M_S2:.17g})"     # noqa: E731
    head, ends = "common.b", []
    for i, u in enumerate(st.units, 1):
        s = states.get(u.unit_id)
        start = f", q(start={u.pump.rated_flow_m3_s:.17g}))"
        inlet = head if st.topology == "series" else "common.b"
        if s and not s.running and s.stopped and s.stopped.mode == "bypass":
            add("PumpStation.StoppedPump", assignment(f"p{i}", resistance=s.stopped.resistance_pa_s2_m6))
            conn.append(f"connect({inlet}, p{i}.a);")
            head = f"p{i}.b"
            continue
        if s and s.valve_opening == 0:
            continue
        add("Strainer", assignment(f"s{i}", resistance=s.suction_resistance if s else 0.0))
        if s and not s.running:
            add("PumpStation.StoppedPump", assignment(f"p{i}", resistance=s.stopped.resistance_pa_s2_m6 if s.stopped else 0.0))
        else:
            factor = 1 - (s.head_loss if s else 0.0)
            pump = u.pump.model_copy(update={"shutoff_head_m": u.pump.shutoff_head_m * factor, "rated_head_m": u.pump.rated_head_m * factor,
                                             "efficiency": u.pump.efficiency * (1 - (s.efficiency_loss if s else 0.0))})
            add("Pump", fluid(pump_args(f"p{i}", pump, point.speed_rpm * (s.speed_ratio if s else 1.0), u.npsh))[:-1] + start)
        add("PumpStation.CheckValve",
            assignment(f"c{i}", resistance=u.check_valve.resistance(rho))[:-1]
            + f", s(start={-0.01 if s and not s.running and st.topology == 'parallel' else u.pump.rated_flow_m3_s:.17g}))")
        add("Pipe", fluid(pipe_args(f"l{i}", u.line)))
        add("Valve", assignment(f"v{i}", diameter=u.valve.diameter_m, fullK=u.valve.full_open_k, opening=s.valve_opening if s else 1.0, rho=rho))
        conn += [f"connect({inlet}, s{i}.a);", f"connect(s{i}.b, p{i}.a);", f"connect(p{i}.b, c{i}.a);",
                 f"connect(c{i}.b, l{i}.a);", f"connect(l{i}.b, v{i}.a);"]
        head = f"v{i}.b"
        ends.append(head)
    ends = [head] if st.topology == "series" else ends
    valve = product.valves[case.control_valve]
    add("Pipe", fluid(pipe_args("otherLine", next(iter(other.pipes.values())), other.elevation_change_m)))
    add("Pipe", fluid(pipe_args("processLine", next(iter(product.pipes.values())), product.elevation_change_m)))
    add("Valve", assignment("valve", diameter=valve.diameter_m, fullK=valve.full_open_k, rho=rho, opening=point.valve_openings.get(case.control_valve, 1.0)))
    add("Strainer", assignment("boosterStrainer", resistance=booster_r))
    b = product.booster
    add("Pump", fluid(pump_args("booster", b.pump, b.speed_rpm, b.npsh))[:-1] + f", q(start={b.pump.rated_flow_m3_s:.17g}))")
    add("Pipe", fluid(pipe_args("delivery", b.discharge_pipe)))
    conn += [f"connect({e}, otherLine.a);" for e in ends]
    conn += ["connect(otherLine.a, processLine.a);", "connect(otherLine.b, otherSink.a);", "connect(processLine.b, valve.a);",
             "connect(valve.b, boosterStrainer.a);", "connect(boosterStrainer.b, booster.a);", "connect(booster.b, delivery.a);",
             "connect(delivery.b, productSink.a);"]
    if all_running(st, states):
        n = range(1, len(st.units) + 1)
        comp.append("    Real massResidual, energyResidual;")
        supplied = "p1.q" if st.topology == "series" else " + ".join(f"p{i}.q" for i in n)
        parts = ["common", "otherLine", "processLine", "valve", "boosterStrainer", "delivery"] + [f"{x}{i}" for i in n for x in "sclv"]
        conn += [f"massResidual = {rho:.17g} * ({supplied} - otherLine.q - booster.q);",
                 "energyResidual = " + " + ".join(f"{x}.shaft * {x}.etaEff" for x in [f"p{i}" for i in n] + ["booster"])
                 + "".join(f" - {x}.q * ({x}.a.p - {x}.b.p)" for x in parts)
                 + " - otherLine.q * (otherSink.p - source.p) - booster.q * (productSink.p - source.p);"]
    conn.append("assert(otherLine.q > 0 and booster.q > 0, \"순방향\");")
    return "\n".join([f"  model {name}", "    import PlantHydraulics.*;", *comp, "  equation", *("    " + c for c in conn), f"  end {name};"])


def all_running(st, states: dict) -> bool:
    return all(s.running and s.valve_opening > 0 for s in states.values())


def expected(case, st, states: dict, r) -> dict[str, float]:
    product = next(b for b in r.branches if b.branch_id == case.downstream_branch)
    other = next(b for b in r.branches if b is not product)
    out = {"otherLine.a.p": r.header_abs_pa, "otherLine.q": other.flow_m3_s, "booster.q": product.flow_m3_s,
           "booster.a.p": product.booster_suction_abs_pa, "booster.b.p": product.booster_discharge_abs_pa, "booster.shaft": product.booster_shaft_w}
    for i, u in enumerate(r.units, 1):
        if u.status == "dead_headed":
            continue
        if u.status != "bypass":
            out[f"p{i}.q"] = u.flow_m3_s
        out[f"p{i}.a.p"], out[f"p{i}.b.p"] = u.suction_abs_pa, u.discharge_abs_pa
        if u.status == "running":
            out[f"p{i}.head"], out[f"p{i}.shaft"], out[f"p{i}.npsha"], out[f"p{i}.margin"] = u.head_m, u.shaft_w, u.npsha_m, u.npsh_margin
            out[f"p{i}.loss"] = u.cavitation_head_loss
    if product.booster_npsha_m is not None:
        out["booster.npsha"], out["booster.margin"], out["booster.loss"] = product.booster_npsha_m, product.booster_npsh_margin, product.booster_head_loss
    if all_running(st, states):
        out["massResidual"], out["energyResidual"] = r.mass_residual_kg_s, r.energy_residual_w
    return out


RESIDUAL = {"massResidual": 1e-6, "energyResidual": 0.1}      # 두 모델 각각 이 안이어야 한다


def row(plant: str, scenario: str, quantity: str, python: float, openmodelica: float) -> dict:
    if not math.isfinite(python) or not math.isfinite(openmodelica):
        raise ValueError(f"{scenario} {quantity}: 유한한 계산값이 필요합니다.")
    err = abs(openmodelica - python)
    tol = RESIDUAL.get(quantity) or max(floor(quantity), 1e-6 * abs(python))
    measured = max(abs(openmodelica), abs(python)) if quantity in RESIDUAL else err
    return {"plant": plant, "scenario": scenario, "quantity": quantity, "python": python, "openmodelica": openmodelica,
            "absolute_error": err, "tolerance": tol, "passed": measured <= tol}


def floor(key: str) -> float:
    return 1e-8 if key.endswith(".q") else 1.0 if key.endswith(".p") else 0.1 if key.endswith("shaft") else 1e-4 if key.endswith("head") else 1e-6


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--omc", required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    args.output.mkdir(parents=True)
    rows = []
    for key in UNITS:
        out = args.output / key
        out.mkdir()
        st, a = build(key)
        models, want = [], {}
        for name, states, extra in scenarios(key, st, a):
            if name not in NAMES:
                continue
            extra2 = dict(extra)
            _, point, _, _ = a.case.scenario(restriction=extra2.pop("restriction", 0.0))
            try:
                r = st.solve(point, states, **extra2)
            except SolveError:
                continue
            ident = name.replace(".", "_")
            models.append(text(ident, a.case, st, point, states, *(extra2.get(f"{x}_suction_resistance", 0.0) for x in ("common", "booster"))))
            want[ident] = expected(a.case, st, states, r)
        for f in ("PlantHydraulics.mo", "PumpStation.mo"):
            shutil.copyfile(HERE / f, out / f)
        (out / "Scenarios.mo").write_text("package Scenarios\n" + "\n".join(models) + "\nend Scenarios;\n", encoding="utf-8")
        cmds = [f'loadFile("{f}"); getErrorString();' for f in ("PlantHydraulics.mo", "PumpStation.mo", "Scenarios.mo")]
        cmds += [f'simulate(Scenarios.{n}, stopTime=1, numberOfIntervals=1, tolerance=1e-10, outputFormat="csv", fileNamePrefix="{n}"); getErrorString();' for n in want]
        (out / "cases.mos").write_text("\n".join(cmds) + "\n", encoding="utf-8")
        version = run_omc(args.omc, out, "--version", "version.log")
        run_omc(args.omc, out, "cases.mos", "cases.log")
        for n, values in want.items():
            got = read_final_row(out / f"{n}_res.csv")
            for q, e in values.items():
                rows.append(row(key, n, q, e, got[q]))
        mine = [r for r in rows if r["plant"] == key]
        print(key, len(want), "사례", sum(r["passed"] for r in mine), "/", len(mine), version)
    with (args.output / "comparison.csv").open("x", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    (args.output / "summary.json").write_text(json.dumps({"passed": all(r["passed"] for r in rows), "total": len(rows)}) + "\n", encoding="utf-8")
    if not all(r["passed"] for r in rows):
        for r in rows:
            if not r["passed"]:
                print(r)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
