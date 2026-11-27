"""CDU 단일 기계 회로(P-8303A 펌프 + NPSH·캐비테이션, F-8201 강제 통풍 팬) 설정을 마스터 운전점에서 역산한다.

정상 해는 마스터 μ(흡입·토출압·유량 / 팬 차압·풍량)를 재현한다. 물성·곡선·배관·증기압·NPSHr 은 synthetic assumption 이다.
실행: `uv run --locked python -m scripts.calibrate_single [--write]`.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import yaml

from common.config_models import load_plant
from simulator.physics.pump import GRAVITY_M_S2
from simulator.physics_single import MMH2O_PA, PRESSURE_PA, SingleCase

ROOT = Path(__file__).resolve().parents[1]
ATM = 101_325.0
R_AIR = 287.05
NORMAL_AIR_DENSITY = 1.2931          # 0 ℃·101.325 kPa 건공기 (Nm3 환산 기준)


def k_for(resistance: float, rho: float, length: float, diameter: float, darcy: float) -> float:
    area = math.pi * diameter**2 / 4
    k = resistance * 2 * area**2 / rho - darcy * length / diameter
    if k < 0:
        raise ValueError("배관 마찰만으로 목표 저항을 넘습니다.")
    return k


def resistance(rho: float, pipe: dict) -> float:
    area = math.pi * pipe["diameter_m"]**2 / 4
    return rho * (pipe["darcy_factor"] * pipe["length_m"] / pipe["diameter_m"] + pipe["minor_loss_k"]) / (2 * area**2)


def bisect(fn, lo: float, hi: float, target: float, increasing: bool) -> float:
    for _ in range(100):
        mid = (lo + hi) / 2
        try:
            value = fn(mid)
        except ValueError:
            value = -math.inf if not increasing else math.inf
        if (value < target) == increasing:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def mu(plant, equip: str, tag: str) -> float:
    return plant.control_by_key()[(equip, tag)][0]


def pump_p8303a() -> tuple[dict, dict]:
    plant = load_plant(ROOT / "config/plant_cdu.yaml")
    rho = 780.0                                       # 약 345 ℃ 상압 잔사유 (synthetic)
    q0 = mu(plant, "P-8303A", "FI-8303A") / 3600
    ps = mu(plant, "P-8303A", "PI-8303A") * PRESSURE_PA["kg/cm2(g)"] + ATM
    pd = mu(plant, "P-8303A", "PI-8303B") * PRESSURE_PA["kg/cm2(g)"] + ATM
    t0 = mu(plant, "P-8303A", "TI-8303B") + 273.15
    suction = {"length_m": 25.0, "diameter_m": 0.30, "darcy_factor": 0.022, "minor_loss_k": 3.0}
    source = ps + resistance(rho, suction) * q0**2   # 상압탑 하부 액위 정압 포함 등가 경계압
    outlet = 7.0 * PRESSURE_PA["kg/cm2(g)"] + ATM     # 잔사유 이송 측 경계 (synthetic)
    discharge = {"length_m": 150.0, "diameter_m": 0.25, "darcy_factor": 0.022}
    discharge["minor_loss_k"] = k_for((pd - outlet) / q0**2, rho, discharge["length_m"], discharge["diameter_m"], 0.022)
    npsha0, npshr = 8.0, 4.0                          # 정상 NPSHa 8 m, 정격 NPSHr 4 m (여유비 2.0, synthetic)
    v = q0 / (math.pi * 0.25**2 / 4)
    pv0 = ps - rho * GRAVITY_M_S2 * (npsha0 - v**2 / (2 * GRAVITY_M_S2))

    def config(b: float, strainer: float, head_loss: float) -> dict:
        return {
            "plant_id": "CDU", "unit_id": "UNIT-CDU-06", "equip_id": "P-8303A", "kind": "pump",
            "provenance": "synthetic assumption", "suction_node": "AT", "discharge_node": "virtual:residue-boundary",
            "atmospheric_pa": ATM, "density_kg_m3": rho, "source_abs_pa": source, "outlet_abs_pa": outlet,
            "machine": {"reference_speed_rpm": 3560, "speed_rpm": 3560, "rated_flow_m3_s": q0,
                        "shutoff_rise_pa": 1.22 * (pd - ps), "rated_rise_pa": pd - ps, "efficiency": 0.72},
            "suction_pipe": suction, "discharge_pipe": discharge,
            "npsh": {"fluid_temp_k": t0, "vapor_pressure_pa": pv0, "vapor_b_k": b, "suction_diameter_m": 0.25,
                     "npshr_rated_m": npshr, "npshr_zero_flow_fraction": 0.5, "onset_margin": 1.3,
                     "steep_coefficient": 0.8, "max_head_loss": 0.6, "efficiency_coupling": 0.5},
            "max_strainer_resistance_pa_s2_m6": strainer, "max_impeller_head_loss": head_loss,
            "max_impeller_efficiency_loss": 0.20, "max_temperature_rise_k": 15.0,
        }

    def solve(cfg, **kw):
        return SingleCase.model_validate(cfg).solve(kw.get("clog", 0), kw.get("wear", 0), kw.get("temp", 0))

    # 1) 증기압 온도 민감도 B: 흡입 온도 +15 K(PM-SUCTION-HOT 의 기존 흡입 유체 온도 상승 폭)에서 캐비테이션으로 유량 −8 %.
    b = bisect(lambda b: solve(config(b, 1, 0.1), temp=1).flow_m3_s / q0, 500, 20000, 0.92, increasing=False)
    # 2) 스트레이너: 이 펌프는 NPSH 여유가 작아 흡입압 경보 하한(1.22)보다 캐비테이션이 먼저 시작된다.
    #    최대 막힘을 여유비 s = 0.85(중간 정도 캐비테이션, 양정 감소 상한 미도달)에 맞춘다.
    target = 0.85
    strainer = bisect(lambda r: solve(config(b, r, 0.1), clog=1).npsh_margin, 1.0, 1e9, target, increasing=False)
    cfg = config(b, float(f"{strainer:.6g}"), 0.10)
    return cfg, {"vapor_b_k": b, "max_clog_npsh_margin": target, "npsha_normal_m": npsha0, "npshr_rated_m": npshr}


def fan_f8201() -> tuple[dict, dict]:
    plant = load_plant(ROOT / "config/plant_cdu.yaml")
    t_in = 298.15
    rho = ATM / (R_AIR * t_in)                        # 25 ℃ 대기 흡입 공기
    q0 = mu(plant, "F-8201", "FI-8201A") * NORMAL_AIR_DENSITY / rho / 3600
    rise = mu(plant, "F-8201", "PDI-8201A") * MMH2O_PA
    speed = mu(plant, "F-8201", "SI-8201A")
    suction = {"length_m": 10.0, "diameter_m": 2.8, "darcy_factor": 0.018, "minor_loss_k": 1.2}   # 흡입 필터·소음기·댐퍼
    inlet = ATM - resistance(rho, suction) * q0**2
    outlet = ATM - 10 * MMH2O_PA                      # 가열로 연소실 통풍압 −10 mmH2O (synthetic)
    discharge = {"length_m": 40.0, "diameter_m": 2.4, "darcy_factor": 0.018}
    discharge["minor_loss_k"] = k_for((inlet + rise - outlet) / q0**2, rho, 40.0, 2.4, 0.018)   # 덕트 + 버너 레지스터

    def config(strainer: float) -> dict:
        return {
            "plant_id": "CDU", "unit_id": "UNIT-CDU-03", "equip_id": "F-8201", "kind": "fan",
            "provenance": "synthetic assumption", "suction_node": "virtual:ambient-air", "discharge_node": "HT",
            "atmospheric_pa": ATM, "density_kg_m3": rho, "flow_reference_density_kg_m3": NORMAL_AIR_DENSITY,
            "source_abs_pa": ATM, "outlet_abs_pa": outlet,
            "machine": {"reference_speed_rpm": speed, "speed_rpm": speed, "rated_flow_m3_s": q0,
                        "shutoff_rise_pa": 1.25 * rise, "rated_rise_pa": rise, "efficiency": 0.80},
            "suction_pipe": suction, "discharge_pipe": discharge,
            "max_strainer_resistance_pa_s2_m6": strainer, "max_impeller_head_loss": 0.15,
            "max_impeller_efficiency_loss": 0.15,
        }

    # 흡입 필터·댐퍼 막힘 최대치: 풍량이 경고 하한 85,000 과 경보 하한 75,000 사이 82,000 Nm3/h.
    strainer = bisect(lambda r: SingleCase.model_validate(config(r)).solve(1, 0, 0).flow_m3_s * rho / NORMAL_AIR_DENSITY * 3600,
                      0.01, 100.0, 82000, increasing=False)
    return config(float(f"{strainer:.6g}")), {"inlet_air_temp_k": t_in, "flow_target_nm3h": 82000}


def dump(name: str, cfg: dict, notes: dict) -> str:
    head = [f"# synthetic assumption: {cfg['plant_id']} {cfg['equip_id']} 단일 {('펌프' if cfg['kind'] == 'pump' else '팬')} 회로. "
            f"scripts/calibrate_single.py 로 생성. 정상 해는 plant_cdu.yaml 운전점 μ 를 재현한다.",
            "# 물성·곡선·배관·경계압" + ("·증기압·NPSHr" if cfg["kind"] == "pump" else "") + " 은 합성 설계 선택이며 현장 자료가 아니다.",
            *(f"#   {k}: {round(v, 4) if isinstance(v, float) else v}" for k, v in notes.items())]
    return "\n".join(head) + "\n" + yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, width=120)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    for name, builder in (("physics_cdu_p8303a.yaml", pump_p8303a), ("physics_cdu_f8201.yaml", fan_f8201)):
        cfg, notes = builder()
        text = dump(name, cfg, notes)
        if args.write:
            (ROOT / "config" / name).write_text(text, encoding="utf-8")
            print("config/" + name)
        else:
            print(text)


if __name__ == "__main__":
    main()
