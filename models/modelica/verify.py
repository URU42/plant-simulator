"""공식 omc로 CDU 모델을 실행하고 Python의 독립 수력 해와 비교한다."""
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
from simulator.physics.network import NetworkResult, OperatingPoint
from simulator.physics.pipe import Pipe
from simulator.physics.pump import GRAVITY_M_S2, Pump
from simulator.physics_adapter import CoupledCase

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
REL_TOL = 1e-6
# 합격 조건은 max(절대 바닥값, |Python 기준값| × 상대 허용오차)다.
ABS_TOL = {
    "feedFlow": 1e-8, "otherFlow": 1e-8, "productFlow": 1e-8,
    "suctionPressure": 1.0, "headerPressure": 1.0, "boosterDischarge": 1.0,
    "feedHead": 1e-4, "feedHydraulic": 0.1, "feedShaft": 0.1, "boosterShaft": 0.1,
}
BALANCE_TOL = {
    "massResidual": 1e-6, "feedPressureResidual": 1.0,
    "otherPressureResidual": 1.0, "productPressureResidual": 1.0, "energyResidual": 0.1,
}


@dataclass(frozen=True)
class Scenario:
    name: str
    speed_ratio: float = 1.0
    opening_ratio: float = 1.0
    blockage: float = 0.0


SCENARIOS = (
    Scenario("Normal"),
    Scenario("ValveRestricted", opening_ratio=0.4),
    Scenario("SpeedLow", speed_ratio=0.9),
    Scenario("SpeedHigh", speed_ratio=1.1),
    Scenario("StrainerBlocked", blockage=1.0),
)


def assignment(prefix: str, **fields: float) -> str:
    values = ", ".join(f"{key}={float(value):.17g}" for key, value in fields.items())
    return f"{prefix}({values})"


def pipe_args(prefix: str, pipe: Pipe, elevation: float = 0.0) -> str:
    return assignment(prefix, length=pipe.length_m, diameter=pipe.diameter_m,
                      darcy=pipe.darcy_factor, minorK=pipe.minor_loss_k, dz=elevation)


def pump_args(prefix: str, pump: Pump, speed: float) -> str:
    return assignment(prefix, referenceRpm=pump.reference_speed_rpm, rpm=speed, eta=pump.efficiency,
                      ratedFlow=pump.rated_flow_m3_s, zeroHead=pump.shutoff_head_m, ratedHead=pump.rated_head_m)


def models(case: CoupledCase) -> tuple[str, dict[str, tuple[OperatingPoint, float]]]:
    network = case.network
    product = next(b for b in network.branches if b.branch_id == case.downstream_branch)
    others = [b for b in network.branches if b is not product]
    if (len(others) != 1 or len(product.pipes) != 1
            or set(product.valves) != {case.control_valve} or product.booster is None):
        raise ValueError("검증 회로에는 제품 분기 한 곳과 별도 단순 분기 한 곳이 필요합니다.")
    other = others[0]
    if len(other.pipes) != 1 or other.valves or other.booster is not None:
        raise ValueError("별도 분기는 배관 하나로 구성되어야 합니다.")
    valve = product.valves[case.control_valve]
    booster = product.booster
    default = case.default_point
    specs = {}
    lines = ["package CduScenarios"]
    for scenario in SCENARIOS:
        speed = default.speed_rpm * scenario.speed_ratio
        opening = default.valve_openings.get(case.control_valve, 1) * scenario.opening_ratio
        resistance = case.max_strainer_resistance_pa_s2_m6 * scenario.blockage
        point = OperatingPoint(suction_abs_pa=default.suction_abs_pa, speed_rpm=speed,
                               valve_openings={case.control_valve: opening})
        specs[scenario.name] = (point, resistance)
        args = [
            f"rho={network.density_kg_m3:.17g}, g={GRAVITY_M_S2:.17g}",
            assignment("source", p=point.suction_abs_pa),
            assignment("otherSink", p=other.outlet_abs_pa),
            assignment("productSink", p=product.outlet_abs_pa),
            assignment("strainer", resistance=resistance),
            pump_args("feed", network.pump, speed),
            pipe_args("otherLine", next(iter(other.pipes.values())), other.elevation_change_m),
            pipe_args("processLine", next(iter(product.pipes.values())), product.elevation_change_m),
            assignment("valve", diameter=valve.diameter_m, fullK=valve.full_open_k, opening=opening),
            pump_args("booster", booster.pump, booster.speed_rpm),
            pipe_args("delivery", booster.discharge_pipe),
        ]
        lines.extend([f"  model {scenario.name}", "    extends CduHydraulics.Circuit(",
                      "      " + ",\n      ".join(args) + ");", f"  end {scenario.name};"])
    lines.append("end CduScenarios;")
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


def python_outputs(case: CoupledCase, point: OperatingPoint, result: NetworkResult) -> dict[str, float]:
    product = next(b for b in result.branches if b.branch_id == case.downstream_branch)
    other = next(b for b in result.branches if b is not product)
    booster = next(b.booster for b in case.network.branches if b.branch_id == case.downstream_branch)
    assert booster is not None and product.booster_shaft_w is not None
    assert product.booster_discharge_abs_pa is not None
    energy = (result.shaft_w * case.network.pump.efficiency + product.booster_shaft_w * booster.pump.efficiency
              - result.pump_flow_m3_s * (point.suction_abs_pa - result.suction_abs_pa)
              - sum(b.flow_m3_s * (b.friction_loss_pa + b.elevation_pressure_pa
                                    + b.outlet_abs_pa - point.suction_abs_pa) for b in result.branches))
    return {
        "feedFlow": result.pump_flow_m3_s, "otherFlow": other.flow_m3_s, "productFlow": product.flow_m3_s,
        "suctionPressure": result.suction_abs_pa, "headerPressure": result.discharge_abs_pa,
        "boosterDischarge": product.booster_discharge_abs_pa, "feedHead": result.pump_head_m,
        "feedHydraulic": result.hydraulic_w, "feedShaft": result.shaft_w, "boosterShaft": product.booster_shaft_w,
        "massResidual": result.mass_residual_kg_s, "feedPressureResidual": result.pump_pressure_residual_pa,
        "otherPressureResidual": other.pressure_residual_pa, "productPressureResidual": product.pressure_residual_pa,
        "energyResidual": energy,
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
    case = CoupledCase.model_validate(load_yaml(config))
    source, specs = models(case)
    output.mkdir(parents=True)
    for name in ("CduHydraulics.mo", "Smoke.mo"):
        shutil.copyfile(HERE / name, output / name)
    (output / "CduScenarios.mo").write_text(source, encoding="utf-8")
    version = run_omc(omc, output, "--version", "version.log")
    commands = ['loadFile("Smoke.mo"); getErrorString();',
                'simulate(Smoke, stopTime=1, numberOfIntervals=10, tolerance=1e-10, outputFormat="csv"); getErrorString();']
    (output / "smoke.mos").write_text("\n".join(commands) + "\n", encoding="utf-8")
    run_omc(omc, output, "smoke.mos", "smoke.log")
    smoke = read_final_row(output / "Smoke_res.csv")["x"]
    if not math.isclose(smoke, math.exp(-1), abs_tol=1e-6, rel_tol=0):
        raise ValueError(f"Smoke 적분 결과가 허용오차를 벗어났습니다. x(1)={smoke}")

    commands = ['loadFile("CduHydraulics.mo"); getErrorString();',
                'loadFile("CduScenarios.mo"); getErrorString();']
    for name in specs:
        commands.append(f'simulate(CduScenarios.{name}, stopTime=1, numberOfIntervals=1, tolerance=1e-10, '
                        f'outputFormat="csv", fileNamePrefix="{name}"); getErrorString();')
    (output / "cases.mos").write_text("\n".join(commands) + "\n", encoding="utf-8")
    run_omc(omc, output, "cases.mos", "cases.log")
    rows = []
    for name, (point, resistance) in specs.items():
        reference = python_outputs(case, point, case.network.solve(point, resistance))
        actual = read_final_row(output / f"{name}_res.csv")
        rows.extend(comparison_row(name, field, expected, actual[field]) for field, expected in reference.items())
    with (output / "comparison.csv").open("x", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    passed = all(r["passed"] for r in rows)
    report = {"omc_version": version, "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
              "relative_tolerance": REL_TOL, "smoke_x_at_1": smoke, "passed": passed, "rows": rows}
    (output / "comparison.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"{version}\n{sum(r['passed'] for r in rows)}/{len(rows)} 항목 통과 · {output}")
    return passed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--omc", default=os.environ.get("OMC", "omc"))
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "physics_cdu_integrated.yaml")
    parser.add_argument("--output", type=Path, required=True, help="새 결과 디렉터리")
    args = parser.parse_args()
    raise SystemExit(0 if compare(args.config, args.omc, args.output) else 1)


if __name__ == "__main__":
    main()
