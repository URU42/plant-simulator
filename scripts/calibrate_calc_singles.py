"""정적 계산(M1) 전용 단일 펌프 회로를 마스터 운전점에서 만든다 (M2 하류 전파, 2026-11-26).

트윈 묶음(physics_*_twin.yaml)에서 제외한 펌프 13대에 P-8303A 와 같은 형식의 단일 회로를 만든다. NPSH 모델은 넣지 않는다.
이 회로는 config/physics_<plant>_calc.yaml 로만 등록하며 수집기 트윈 런타임은 읽지 않는다(실시간 데이터·AE 기준 불변).
정상 해는 마스터 μ(흡입·토출압·유량)를 재현한다. 물성·곡선·배관·경계압은 모두 synthetic assumption 이다.
흡입압 태그가 없는 펌프는 흡입 경계압을 가정값으로 둔다. 윤활유 펌프 P-2501 은 oil_pressure 를 토출압으로 본다.
실행: `uv run --locked python -m scripts.calibrate_calc_singles [--write]`.
"""
from __future__ import annotations

import argparse
import math

import yaml

from common.config_models import load_plant
from scripts.calibrate_single import ATM, ROOT, k_for, resistance
from simulator.physics_single import PRESSURE_PA, SingleCase

# (공장, 설비, 밀도 kg/m3, 가정 흡입 게이지압(흡입압 태그 없을 때), 흡입·토출 노드, 설명) — 모두 synthetic assumption
PUMPS = [
    ("cdu", "P-8102A", 1000.0, 2.0, "virtual:wash-water", "DS", "탈염 세정수"),
    ("cdu", "P-8301A", 720.0, None, "AT", "HX", "중간 순환환류"),
    ("cdu", "P-8302A", 690.0, None, "DR", "AT", "탑정 환류"),
    ("hou", "P-2501", 870.0, 0.3, "virtual:lube-oil-reservoir", "virtual:bearing-header", "압축기 윤활유"),
    ("hou", "P-2601", 700.0, None, "FRAC", "FRAC", "분별탑 환류"),
    ("rfcc", "P-1301A", 950.0, None, "FR", "FR", "슬러리 펌프어라운드"),
    ("rfcc", "P-1302A", 860.0, None, "FR", "FR", "LCO 펌프어라운드"),
    ("rfcc", "P-1402A", 750.0, None, "GR", "GR", "흡수유"),
    ("rfcc", "P-1403A", 560.0, None, "GR", "GR", "가스 회수 환류"),
    ("rfcc", "P-1501A", 520.0, None, "GR", "virtual:lpg-product", "LPG 이송"),
    ("rfcc", "P-1502A", 700.0, None, "GR", "virtual:gasoline-product", "가솔린 이송"),
    ("rhds", "P-3103", 700.0, None, "FR", "FR", "분별탑 환류"),
    ("vrds", "P-4103", 700.0, None, "FR", "FR", "분별탑 환류"),
]
# 정적 계산 전용 고장 적용 확장(physics_<plant>_calc.yaml faults). 회로가 이미 그 물리 파라미터를 계산하는 설비만 넣는다.
#  - 단일 회로 펌프: 임펠러 마모(impeller_wear)
#  - 결합 회로 첫 펌프: 제품 제어밸브 개도 이상(valve_restriction, 처리량 감소 고장과 같은 밸브). CDU 는 fault_modes.yaml 에 이미 있다
FEED_VALVE = {"hou": "P-2101", "rfcc": "P-1101A", "rhds": "P-3101", "vrds": "P-4101"}
STATIC_SHARE = 0.4          # 토출 경계 정압 = 흡입압 + 양정의 40 % (나머지는 배관·제어밸브 손실)
STRAINER_SHARE = 0.3        # 최대 막힘: 정상 유량에서 흡입 경계 절대압의 30 % 손실
SHUTOFF = 1.25              # 체절 압력 상승 / 정격 압력 상승


def pipe_for(q: float, velocity: float, length: float, k: float | None = None) -> dict:
    d = round(math.sqrt(4 * q / (math.pi * velocity)), 3)
    return {"length_m": length, "diameter_m": d, "darcy_factor": 0.02, **({"minor_loss_k": k} if k is not None else {})}


def build(key: str, equip: str, rho: float, assumed_suction: float | None, s_node: str, d_node: str) -> dict:
    plant = load_plant(ROOT / f"config/plant_{key}.yaml")
    e = plant.equipment_by_id()[equip]
    mu = plant.control_by_key()
    tag = {t.signal: t for t in e.tags}
    q0 = mu[(equip, tag["flow"].tag_id)][0] / 3600
    out_tag = tag.get("discharge_pressure") or tag["oil_pressure"]
    pa = PRESSURE_PA[out_tag.unit]
    pd = mu[(equip, out_tag.tag_id)][0] * pa + ATM
    ps = (mu[(equip, tag["suction_pressure"].tag_id)][0] if "suction_pressure" in tag else assumed_suction) * pa + ATM
    suction = pipe_for(q0, 1.5, 15.0, 2.0)
    source = ps + resistance(rho, suction) * q0**2
    outlet = ps + STATIC_SHARE * (pd - ps)
    discharge = pipe_for(q0, 2.5, 80.0)
    discharge["minor_loss_k"] = k_for((pd - outlet) / q0**2, rho, discharge["length_m"], discharge["diameter_m"], 0.02)
    return {
        "plant_id": plant.plant.plant_id, "unit_id": e.unit_id, "equip_id": equip, "kind": "pump",
        "provenance": "synthetic assumption", "suction_node": s_node, "discharge_node": d_node,
        "atmospheric_pa": ATM, "density_kg_m3": rho, "source_abs_pa": source, "outlet_abs_pa": outlet,
        "machine": {"reference_speed_rpm": 3560, "speed_rpm": 3560, "rated_flow_m3_s": q0,
                    "shutoff_rise_pa": SHUTOFF * (pd - ps), "rated_rise_pa": pd - ps, "efficiency": 0.70},
        "suction_pipe": suction, "discharge_pipe": discharge,
        "max_strainer_resistance_pa_s2_m6": float(f"{STRAINER_SHARE * source / q0**2:.6g}"),
        "max_impeller_head_loss": 0.10, "max_impeller_efficiency_loss": 0.20,
    }


def file_name(key: str, equip: str) -> str:
    return f"physics_{key}_{equip.lower().replace('-', '')}.yaml"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    lists: dict[str, list[tuple[str, str, str, bool]]] = {}
    for key, equip, rho, assumed, s_node, d_node, label in PUMPS:
        cfg = build(key, equip, rho, assumed, s_node, d_node)
        r = SingleCase.model_validate(cfg).solve()
        assert abs(r.flow_m3_s / cfg["machine"]["rated_flow_m3_s"] - 1) < 1e-6, equip       # 정상 해 = μ
        name = file_name(key, equip)
        text = (f"# synthetic assumption: {cfg['plant_id']} {equip} 단일 펌프 회로({label}), 정적 계산(M1) 전용.\n"
                "# scripts/calibrate_calc_singles.py 로 생성. 정상 해는 마스터 운전점 μ 를 재현한다. 수집기 트윈은 읽지 않는다.\n"
                "# 물성·곡선·배관·경계압은 합성 설계 선택이며 현장 자료가 아니다. 탑·드럼 수력과 순환 루프는 계산하지 않는다.\n"
                + (f"#   흡입압 태그 없음: 흡입 경계 게이지압 {assumed:g} 은 가정값\n" if assumed is not None else "")
                + yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, width=120))
        lists.setdefault(key, []).append((name, equip, label, assumed is not None))
        if args.write:
            (ROOT / "config" / name).write_text(text, encoding="utf-8")
            print("config/" + name)
        else:
            print(text)
    for key, items in lists.items():
        lines = [f"# {key.upper()} 정적 계산(M1) 전용 단일 회로 목록 (scripts/calibrate_calc_singles.py 생성, synthetic assumption).",
                 "# 수집기 트윈(physics_*_twin.yaml)과 AE 기준에는 쓰지 않는다. 시뮬레이션 화면 계산에서만 읽는다.",
                 "circuits:", *(f"  - {n}" for n, *_ in items),
                 "notes:",
                 *(f"  {e}: \"정적 계산 전용 대용 회로({label}): 펌프 곡선·배관 저항은 정상 운전점에서 역산한 가정값입니다"
                   + (", 흡입 경계압은 태그가 없어 가정값입니다" if assumed else "")
                   + ". 탑·드럼 수력과 순환 루프는 계산하지 않습니다.\"" for _, e, label, assumed in items),
                 "faults:",
                 "  PM-IMPELLER-WEAR: [" + ", ".join(e for _, e, *_ in items) + "]",
                 *([f"  PM-DISCHARGE-VALVE-RESTRICTION: [{FEED_VALVE[key]}]"] if key in FEED_VALVE else [])]
        path = ROOT / "config" / f"physics_{key}_calc.yaml"
        if args.write:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            print(f"config/{path.name}")


if __name__ == "__main__":
    main()
