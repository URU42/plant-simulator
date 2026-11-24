"""공식 omc로 플랜트 대표 수력 회로(PlantHydraulics)를 실행하고 Python의 독립 수력 해와 비교한다."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from common.config_models import load_yaml
from simulator.physics.network import Network, NetworkResult, OperatingPoint
from simulator.physics.pipe import Pipe
from simulator.physics.pump import GRAVITY_M_S2, Pump  # noqa: F401
from simulator.physics_adapter import CoupledCase
from simulator.physics_single import SingleCase

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
REL_TOL = 1e-6
# 합격 조건은 max(절대 바닥값, |Python 기준값| × 상대 허용오차)다.
ABS_TOL = {
    "feedFlow": 1e-8, "otherFlow": 1e-8, "productFlow": 1e-8,
    "suctionPressure": 1.0, "headerPressure": 1.0, "boosterSuction": 1.0, "boosterDischarge": 1.0,
    "feedHead": 1e-4, "feedHydraulic": 0.1, "feedShaft": 0.1, "boosterShaft": 0.1,
    "feedNpsha": 1e-6, "feedNpshr": 1e-6, "feedMargin": 1e-6, "feedLoss": 1e-6,
    "boosterNpsha": 1e-6, "boosterNpshr": 1e-6, "boosterMargin": 1e-6, "boosterLoss": 1e-6,
}
BALANCE_TOL = {
    "massResidual": 1e-6, "feedPressureResidual": 1.0,
    "otherPressureResidual": 1.0, "productPressureResidual": 1.0, "energyResidual": 0.1,
}


@dataclass(frozen=True)
class Scenario:
    name: str
    speed_ratio: float = 1.0
    blockage: float = 0.0
    wear: float = 0.0
    valve_restriction: float = 0.0
    booster_blockage: float = 0.0
    booster_wear: float = 0.0

    def amounts(self) -> tuple[float, float, float, float, float]:
        return self.blockage, self.wear, self.valve_restriction, self.booster_blockage, self.booster_wear


SCENARIOS = (
    Scenario("Normal"),
    Scenario("ValveRestricted", valve_restriction=1.0),
    Scenario("ThroughputReduced", valve_restriction=0.2),
    Scenario("ImpellerWorn", wear=1.0),
    Scenario("SpeedLow", speed_ratio=0.9),
    Scenario("SpeedHigh", speed_ratio=1.1),
    Scenario("StrainerBlocked", blockage=1.0),
    Scenario("BoosterStrainerBlocked", booster_blockage=1.0),
    Scenario("BoosterWorn", booster_wear=1.0),
)


def assignment(prefix: str, **fields: float) -> str:
    values = ", ".join(f"{key}={float(value):.17g}" for key, value in fields.items())
    return f"{prefix}({values})"


def pipe_args(prefix: str, pipe: Pipe, elevation: float = 0.0) -> str:
    return assignment(prefix, length=pipe.length_m, diameter=pipe.diameter_m,
                      darcy=pipe.darcy_factor, minorK=pipe.minor_loss_k, dz=elevation)


def pump_args(prefix: str, pump: Pump, speed: float, npsh=None) -> str:
    fields = dict(referenceRpm=pump.reference_speed_rpm, rpm=speed, eta=pump.efficiency,
                  ratedFlow=pump.rated_flow_m3_s, zeroHead=pump.shutoff_head_m, ratedHead=pump.rated_head_m)
    if npsh is None:
        return assignment(prefix, **fields)
    fields.update(fluidTemp=npsh.fluid_temp_k, refTemp=npsh.fluid_temp_k, vaporRef=npsh.vapor_pressure_pa,
                  vaporB=npsh.vapor_b_k, suctionDiameter=npsh.suction_diameter_m, npshrRated=npsh.npshr_rated_m,
                  zeroFlowFraction=npsh.npshr_zero_flow_fraction, onsetMargin=npsh.onset_margin,
                  steep=npsh.steep_coefficient, maxLoss=npsh.max_head_loss, effCoupling=npsh.efficiency_coupling)
    return assignment(prefix, **fields)[:-1] + ", cavitation=true)"


Spec = tuple[Network, OperatingPoint, float, float]


def models(case: CoupledCase) -> tuple[str, dict[str, Spec]]:
    network = case.network
    product = next(b for b in network.branches if b.branch_id == case.downstream_branch)
    others = [b for b in network.branches if b is not product]
    if (len(others) != 1 or len(product.pipes) != 1
            or set(product.valves) != {case.control_valve} or product.booster is None):
        raise ValueError("검증 회로에는 제품 분기 한 곳과 별도 단순 분기 한 곳이 필요합니다.")
    other = others[0]
    if len(other.pipes) != 1 or other.valves or other.booster is not None:
        raise ValueError("별도 분기는 배관 하나로 구성되어야 합니다.")
    specs: dict[str, Spec] = {}
    # 시작 추정값은 설계 정격 유량(설정 입력)이다. Python 해를 넘기지 않는다.
    feed_start = network.pump.rated_flow_m3_s
    product_start = product.booster.pump.rated_flow_m3_s
    other_start = max(feed_start - product_start, 0.1 * feed_start)
    lines = ["package Scenarios"]
    for scenario in SCENARIOS:
        net, point, resistance, booster_resistance = case.scenario(*scenario.amounts())
        point = point.model_copy(update={"speed_rpm": point.speed_rpm * scenario.speed_ratio})
        specs[scenario.name] = (net, point, resistance, booster_resistance)
        branch = next(b for b in net.branches if b.branch_id == case.downstream_branch)
        valve = branch.valves[case.control_valve]
        args = [
            f"rho={net.density_kg_m3:.17g}, g={GRAVITY_M_S2:.17g}",
            f"feedStart={feed_start:.17g}, otherStart={other_start:.17g}, productStart={product_start:.17g}",
            assignment("source", p=point.suction_abs_pa),
            assignment("otherSink", p=other.outlet_abs_pa),
            assignment("productSink", p=product.outlet_abs_pa),
            assignment("strainer", resistance=resistance),
            pump_args("feed", net.pump, point.speed_rpm, net.pump_npsh),
            pipe_args("otherLine", next(iter(other.pipes.values())), other.elevation_change_m),
            pipe_args("processLine", next(iter(product.pipes.values())), product.elevation_change_m),
            assignment("valve", diameter=valve.diameter_m, fullK=valve.full_open_k,
                       opening=point.valve_openings.get(case.control_valve, 1.0)),
            assignment("boosterStrainer", resistance=booster_resistance),
            pump_args("booster", branch.booster.pump, branch.booster.speed_rpm, branch.booster.npsh),
            pipe_args("delivery", branch.booster.discharge_pipe),
        ]
        lines.extend([f"  model {scenario.name}", "    extends PlantHydraulics.Circuit(",
                      "      " + ",\n      ".join(args) + ");", f"  end {scenario.name};"])
    lines.append("end Scenarios;")
    return "\n".join(lines) + "\n", specs


def run_omc(omc: str, output: Path, script: str, log_name: str) -> str:
    log = output / log_name
    try:
        run = subprocess.run([omc, script], cwd=output, capture_output=True, text=True,
                             timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.write_text(str(exc), encoding="utf-8")
        raise RuntimeError(f"OpenModelica 실행 실패. 로그 {log}") from exc
    log.write_text(run.stdout + run.stderr, encoding="utf-8")
    if run.returncode or "Error:" in run.stdout or "Error:" in run.stderr:
        raise RuntimeError(f"OpenModelica 실행 실패. 로그 {log}")
    return run.stdout.strip()


def read_final_row(file: Path) -> dict[str, float]:
    with file.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) < 2 or not math.isclose(float(rows[-1]["time"]), 1.0, abs_tol=1e-12):
        raise ValueError(f"종료 시각 1초의 시뮬레이션 결과가 없습니다. {file}")
    return {k: float(v) for k, v in rows[-1].items()}


def python_outputs(case: CoupledCase, network: Network, point: OperatingPoint, result: NetworkResult) -> dict[str, float]:
    product = next(b for b in result.branches if b.branch_id == case.downstream_branch)
    other = next(b for b in result.branches if b is not product)
    booster = next(b.booster for b in network.branches if b.branch_id == case.downstream_branch)
    assert booster is not None and product.booster_shaft_w is not None
    assert product.booster_discharge_abs_pa is not None and product.booster_suction_abs_pa is not None
    energy = (result.hydraulic_w + product.booster_hydraulic_w
              - result.pump_flow_m3_s * (point.suction_abs_pa - result.suction_abs_pa)
              - sum(b.flow_m3_s * (b.friction_loss_pa + b.elevation_pressure_pa
                                    + b.outlet_abs_pa - point.suction_abs_pa) for b in result.branches))
    return {
        "feedFlow": result.pump_flow_m3_s, "otherFlow": other.flow_m3_s, "productFlow": product.flow_m3_s,
        "suctionPressure": result.suction_abs_pa, "headerPressure": result.discharge_abs_pa,
        "boosterSuction": product.booster_suction_abs_pa,
        "boosterDischarge": product.booster_discharge_abs_pa, "feedHead": result.pump_head_m,
        "feedHydraulic": result.hydraulic_w, "feedShaft": result.shaft_w, "boosterShaft": product.booster_shaft_w,
        "massResidual": result.mass_residual_kg_s, "feedPressureResidual": result.pump_pressure_residual_pa,
        "otherPressureResidual": other.pressure_residual_pa, "productPressureResidual": product.pressure_residual_pa,
        "energyResidual": energy,
        **({"feedNpsha": result.pump_npsha_m, "feedNpshr": result.pump_npshr_m, "feedMargin": result.pump_npsh_margin,
            "feedLoss": result.pump_head_loss} if result.pump_npsha_m is not None else {}),
        **({"boosterNpsha": product.booster_npsha_m, "boosterNpshr": product.booster_npshr_m,
            "boosterMargin": product.booster_npsh_margin, "boosterLoss": product.booster_head_loss}
           if product.booster_npsha_m is not None else {}),
    }


def comparison_row(name: str, field: str, expected: float, measured: float) -> dict:
    if not math.isfinite(measured) or not math.isfinite(expected):
        raise ValueError(f"{name} {field}: 유한한 계산값이 필요합니다.")
    error = abs(measured - expected)
    if field in BALANCE_TOL:
        limit = BALANCE_TOL[field]
        passed = max(abs(measured), abs(expected)) <= limit
        relative = None
    else:
        limit = max(ABS_TOL[field], REL_TOL * abs(expected))
        passed = error <= limit
        relative = error / abs(expected) if expected else None
    return {"scenario": name, "quantity": field, "python": expected, "openmodelica": measured,
            "absolute_error": error, "relative_error": relative, "tolerance": limit, "passed": passed}


def compare(config: Path, omc: str, output: Path) -> bool:
    if "machine" in load_yaml(config):
        return compare_single(config, omc, output)
    case = CoupledCase.model_validate(load_yaml(config))
    source, specs = models(case)
    output.mkdir(parents=True)
    for name in ("PlantHydraulics.mo", "Smoke.mo"):
        shutil.copyfile(HERE / name, output / name)
    (output / "Scenarios.mo").write_text(source, encoding="utf-8")
    version = run_omc(omc, output, "--version", "version.log")
    commands = ['loadFile("Smoke.mo"); getErrorString();',
                'simulate(Smoke, stopTime=1, numberOfIntervals=10, tolerance=1e-10, outputFormat="csv"); getErrorString();']
    (output / "smoke.mos").write_text("\n".join(commands) + "\n", encoding="utf-8")
    run_omc(omc, output, "smoke.mos", "smoke.log")
    smoke = read_final_row(output / "Smoke_res.csv")["x"]
    if not math.isclose(smoke, math.exp(-1), abs_tol=1e-6, rel_tol=0):
        raise ValueError(f"Smoke 적분 결과가 허용오차를 벗어났습니다. x(1)={smoke}")

    commands = ['loadFile("PlantHydraulics.mo"); getErrorString();',
                'loadFile("Scenarios.mo"); getErrorString();']
    for name in specs:
        commands.append(f'simulate(Scenarios.{name}, stopTime=1, numberOfIntervals=1, tolerance=1e-10, '
                        f'outputFormat="csv", fileNamePrefix="{name}"); getErrorString();')
    (output / "cases.mos").write_text("\n".join(commands) + "\n", encoding="utf-8")
    run_omc(omc, output, "cases.mos", "cases.log")
    rows = []
    for name, (network, point, resistance, booster_resistance) in specs.items():
        reference = python_outputs(case, network, point, network.solve(point, resistance, booster_resistance))
        actual = read_final_row(output / f"{name}_res.csv")
        rows.extend(comparison_row(name, field, expected, actual[field]) for field, expected in reference.items())
    with (output / "comparison.csv").open("x", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    passed = all(r["passed"] for r in rows)
    report = {"omc_version": version, "config": config.name, "plant_id": case.plant_id,
              "circuit": [case.equip_id, case.downstream_path[-1]],
              "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
              "relative_tolerance": REL_TOL, "smoke_x_at_1": smoke, "passed": passed, "rows": rows}
    (output / "comparison.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"{version}\n{sum(r['passed'] for r in rows)}/{len(rows)} 항목 통과 · {output}")
    return passed


PLANTS = ("cdu", "hou", "rfcc", "rhds", "vrds")
CDU_D0 = ("physics_cdu_integrated.yaml", "physics_cdu_p8303a.yaml", "physics_cdu_f8201.yaml")

# --- 단일 기계 회로 (P-8303A NPSH·캐비테이션, F-8201 팬) ------------------------------------
SINGLE_ABS_TOL = {"volumeFlow": 1e-8, "inletPressure": 1.0, "outletPressure": 1.0, "rise": 1.0, "hydraulic": 0.1,
                  "shaft": 0.1, "npsha": 1e-6, "npshr": 1e-6, "margin": 1e-6, "headLoss": 1e-6}
SINGLE_BALANCE_TOL = {"pressureResidual": 1.0, "energyResidual": 0.1}


@dataclass(frozen=True)
class SingleScenario:
    name: str
    clog: float = 0.0
    wear: float = 0.0
    temp: float = 0.0
    speed_ratio: float = 1.0
    cavitation_only: bool = False


SINGLE_SCENARIOS = (
    SingleScenario("Normal"), SingleScenario("SpeedLow", speed_ratio=0.9), SingleScenario("SpeedHigh", speed_ratio=1.05),
    SingleScenario("StrainerHalf", clog=0.5), SingleScenario("StrainerBlocked", clog=1.0), SingleScenario("Worn", wear=1.0),
    SingleScenario("HotOnset", temp=0.5, cavitation_only=True), SingleScenario("Hot", temp=1.0, cavitation_only=True),
    SingleScenario("HotStrainer", clog=0.55, temp=1.0, cavitation_only=True),
)


def single_models(case: SingleCase) -> tuple[str, dict]:
    specs, lines = {}, ["package Scenarios"]
    for sc in SINGLE_SCENARIOS:
        if sc.cavitation_only and case.npsh is None:
            continue
        scaled, strainer, temp = case.scenario(sc.clog, sc.wear, sc.temp)
        scaled = scaled.model_copy(update={"machine": scaled.machine.model_copy(
            update={"speed_rpm": scaled.machine.speed_rpm * sc.speed_ratio})})
        specs[sc.name] = (scaled, strainer, temp)
        m, n = scaled.machine, scaled.npsh
        machine = dict(referenceRpm=m.reference_speed_rpm, rpm=m.speed_rpm, eta=m.efficiency, ratedFlow=m.rated_flow_m3_s,
                       zeroRise=m.shutoff_rise_pa, ratedRise=m.rated_rise_pa)
        if n is not None:
            machine.update(fluidTemp=temp, refTemp=n.fluid_temp_k, vaporRef=n.vapor_pressure_pa, vaporB=n.vapor_b_k,
                           suctionDiameter=n.suction_diameter_m, npshrRated=n.npshr_rated_m,
                           zeroFlowFraction=n.npshr_zero_flow_fraction, onsetMargin=n.onset_margin,
                           steep=n.steep_coefficient, maxLoss=n.max_head_loss, effCoupling=n.efficiency_coupling)
        machine_args = assignment("machine", **machine)
        if n is not None:
            machine_args = machine_args[:-1] + ", cavitation=true)"
        args = [f"rho={scaled.density_kg_m3:.17g}, g={GRAVITY_M_S2:.17g}, qStart={m.rated_flow_m3_s:.17g}",
                assignment("source", p=scaled.source_abs_pa), assignment("sink", p=scaled.outlet_abs_pa),
                assignment("strainer", resistance=strainer), pipe_args("suctionLine", scaled.suction_pipe),
                machine_args, pipe_args("dischargeLine", scaled.discharge_pipe)]
        lines.extend([f"  model {sc.name}", "    extends PlantHydraulics.SingleCircuit(",
                      "      " + ",\n      ".join(args) + ");", f"  end {sc.name};"])
    lines.append("end Scenarios;")
    return "\n".join(lines) + "\n", specs


def single_outputs(case: SingleCase, strainer: float, temp: float) -> dict[str, float]:
    r = case.solve_point(strainer, temp)
    out = {"volumeFlow": r.flow_m3_s, "inletPressure": r.inlet_abs_pa, "outletPressure": r.outlet_abs_pa, "rise": r.rise_pa,
           "hydraulic": r.hydraulic_w, "shaft": r.shaft_w, "pressureResidual": r.pressure_residual_pa,
           "energyResidual": r.energy_residual_w}
    if case.npsh is not None:
        out.update(npsha=r.npsha_m, npshr=r.npshr_m, margin=r.npsh_margin, headLoss=r.head_loss)
    return out


def single_row(name: str, field: str, expected: float, measured: float) -> dict:
    if not math.isfinite(measured) or not math.isfinite(expected):
        raise ValueError(f"{name} {field}: 유한한 계산값이 필요합니다.")
    error = abs(measured - expected)
    if field in SINGLE_BALANCE_TOL:
        limit, relative = SINGLE_BALANCE_TOL[field], None
        passed = max(abs(measured), abs(expected)) <= limit
    else:
        limit = max(SINGLE_ABS_TOL[field], REL_TOL * abs(expected))
        passed, relative = error <= limit, (error / abs(expected) if expected else None)
    return {"scenario": name, "quantity": field, "python": expected, "openmodelica": measured, "absolute_error": error,
            "relative_error": relative, "tolerance": limit, "passed": passed}


def compare_single(config: Path, omc: str, output: Path) -> bool:
    case = SingleCase.model_validate(load_yaml(config))
    source, specs = single_models(case)
    output.mkdir(parents=True)
    for name in ("PlantHydraulics.mo", "Smoke.mo"):
        shutil.copyfile(HERE / name, output / name)
    (output / "Scenarios.mo").write_text(source, encoding="utf-8")
    version = run_omc(omc, output, "--version", "version.log")
    commands = ['loadFile("PlantHydraulics.mo"); getErrorString();', 'loadFile("Scenarios.mo"); getErrorString();']
    for name in specs:
        commands.append(f'simulate(Scenarios.{name}, stopTime=1, numberOfIntervals=1, tolerance=1e-10, '
                        f'outputFormat="csv", fileNamePrefix="{name}"); getErrorString();')
    (output / "cases.mos").write_text("\n".join(commands) + "\n", encoding="utf-8")
    run_omc(omc, output, "cases.mos", "cases.log")
    rows = []
    for name, (scaled, strainer, temp) in specs.items():
        actual = read_final_row(output / f"{name}_res.csv")
        rows.extend(single_row(name, f, v, actual[f]) for f, v in single_outputs(scaled, strainer, temp).items())
    with (output / "comparison.csv").open("x", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    passed = all(r["passed"] for r in rows)
    report = {"omc_version": version, "config": config.name, "plant_id": case.plant_id, "circuit": [case.equip_id],
              "kind": case.kind, "cavitation_model": case.npsh is not None,
              "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(), "relative_tolerance": REL_TOL,
              "passed": passed, "rows": rows}
    (output / "comparison.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"{version}\n{sum(r['passed'] for r in rows)}/{len(rows)} 항목 통과 · {output}")
    return passed



def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--omc", default=os.environ.get("OMC", "omc"))
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "physics_cdu_integrated.yaml")
    parser.add_argument("--all", action="store_true", help="다섯 플랜트 설정을 <output>/<plant> 에 각각 비교한다")
    parser.add_argument("--cdu-d0", action="store_true", help="CDU D0 회로 묶음(결합·P-8303A·F-8201)을 각각 비교한다")
    parser.add_argument("--output", type=Path, required=True, help="새 결과 디렉터리")
    args = parser.parse_args()
    if args.cdu_d0:
        args.output.mkdir(parents=True)
        # 기존 Smoke 실행은 compare 안에서 회로마다 따로 하지 않으므로 묶음 앞에서 한 번 확인한다.
        results = {name: compare(ROOT / "config" / name, args.omc, args.output / Path(name).stem) for name in CDU_D0}
        (args.output / "summary.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
        for name, passed in results.items():
            print(f"{name}: {'PASS' if passed else 'FAIL'}")
        raise SystemExit(0 if all(results.values()) else 1)
    if not args.all:
        raise SystemExit(0 if compare(args.config, args.omc, args.output) else 1)
    args.output.mkdir(parents=True)
    results = {key: compare(ROOT / "config" / f"physics_{key}_integrated.yaml", args.omc, args.output / key)
               for key in PLANTS}
    (args.output / "summary.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    for key, passed in results.items():
        print(f"{key}: {'PASS' if passed else 'FAIL'}")
    raise SystemExit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
