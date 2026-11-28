"""D9 가설 검증 평가용 관측값 생성 (synthetic, 진단 엔진과 다른 교란 모델).

실행: `python -m scripts.d9_observations <출력.json> [--perturb 0.03] [--seed 7]`

같은 엔진으로 관측을 만들고 진단하면(inverse crime) 정확도가 부풀려진다. 그래서 설정을 임시 폴더에 복사해
펌프 체절 양정 ×(1+k), 배관 국부 손실 ×(1+2k), 효율 ×(1−|k|/2) 로 바꾼 '다른 공장'에서 값을 만들고,
태그별 정상 σ(plant_*.yaml control)로 잡음을 더한다. 진단은 공칭 설정의 계산 서비스로 한다.
정적 계산은 '마스터 정상값 + 모델 변화량'으로 값을 내므로 회로 교란은 정상점을 옮기지 못한다. 그래서 태그마다
정상점 편차(±k/2 균등, 사례 사이 고정)를 따로 곱해(항등식으로 묶인 계측은 같은 편차, 2026-11-28) 모델과 실제 운전점의 차이도 흉내 낸다.
출력은 현재값 API 모양(values·stats·missing)과 마스터 단위, 정답(cause)이다. 정답은 평가에서만 읽는다.
"""
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path

import numpy as np

from common.config_models import load_yaml
from simulator.static_calc import PlantCalc

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
SAMPLES = 600            # 10분 창, 1초 표본
EFFECTIVE = 60           # 자기상관을 감안한 평균의 유효 표본 수

# (사례 id, 공장, 분석 설비, 원인 값들, 기대 가설, 기대 위치, 센서 편향)
CASES = [
    ("cdu-normal", "cdu", "P-8101A", {}, "normal_change", None, None),
    ("cdu-a-suction", "cdu", "P-8101A", {"P-8101A/strainer_blockage": 0.7}, "suction_loss", "P-8101A", None),
    ("cdu-b-suction", "cdu", "P-8101A", {"P-8101B/strainer_blockage": 0.7}, "suction_loss", "P-8101B", None),
    ("cdu-a-wear", "cdu", "P-8101A", {"P-8101A/impeller_wear": 0.8}, "impeller_wear", "P-8101A", None),
    ("cdu-down-wear", "cdu", "P-8101A", {"P-8401A/impeller_wear": 0.8}, "impeller_wear", "P-8401A", None),
    # 토출 밸브 0.55 는 유량 제어(FIC)가 보상해 계측이 정상과 같다(관측 불가, d9 문서). 밸브 이상은 제어밸브 고착으로 본다.
    ("cdu-a-valve-hidden", "cdu", "P-8101A", {"P-8101A/discharge_valve_opening": 0.55}, "normal_change", None, None),
    ("cdu-valve-stuck", "cdu", "P-8101A", {"coupled:P-8101A/control_valve_opening": 0.4}, "valve_fault", None, None),
    ("cdu-a-speed", "cdu", "P-8101A", {"P-8101A/speed_ratio": 0.94}, "normal_change", "P-8101A", None),
    ("cdu-a-sensor", "cdu", "P-8101A", {}, "sensor_fault", None, ("P-8101A", "PI-8101A", 1.15)),
    ("hou-s1-suction", "hou", "P-2101", {"P-2101/strainer_blockage": 0.7}, "suction_loss", "P-2101", None),
    ("hou-s2-wear", "hou", "P-2101", {"P-2101-S2/impeller_wear": 0.8}, "impeller_wear", "P-2101-S2", None),
    ("hou-down-suction", "hou", "P-2101", {"P-2701/strainer_blockage": 0.6}, "suction_loss", "P-2701", None),
]


# 같은 물리량에 묶인 계측은 정상점 편차를 함께 쓴다(2026-11-28). 태그마다 따로 옮기면 물리 항등식
# (스트레이너 앞 압력 − 흡입 압력 = 차압, 이중화 토출 압력 쌍, 헤더 유량 = 병렬 유량 합, 직렬 1단 = 2단 유량)이 깨져
# AI 계기 일관성 검사가 모든 사례를 '계기 이상'으로 본다(교란 ±3 % 에서 12/12). 모두 선형이라 같은 배율이면 유지된다.
FLOW = {"flow", "header_flow"}
SUCTION = {"suction_pressure", "strainer_inlet_pressure", "strainer_dp"}
DISCHARGE = {"discharge_pressure", "redundant_discharge_pressure"}


def offset_group(calc: PlantCalc, plant: str, key: tuple) -> tuple:
    signal = calc.tags[key].signal if key in calc.tags else None
    if signal in FLOW:
        return (plant, calc.circuit_of.get(key[0]) or key[0], "flow")
    if signal in SUCTION:
        return (plant, key[0], "suction")
    if signal in DISCHARGE:
        return (plant, key[0], "discharge")
    return (plant, *key)


def perturb(node, k: float):
    if isinstance(node, dict):
        out = {}
        for key, v in node.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                v = v * (1 + k) if key == "shutoff_head_m" else v * (1 + 2 * k) if key == "minor_loss_k" \
                    else v * (1 - abs(k) / 2) if key == "efficiency" else v
            out[key] = perturb(v, k)
        # 큰 음의 교란에서 체절 양정이 정격 양정 아래로 내려가면 설정 검증에 걸린다(open-issues 116).
        # 그 펌프는 양정 곡선 전체(정격 양정 포함)를 같은 비율로 줄인다. 그 밖의 경우는 이전 평가와 같다.
        if "shutoff_head_m" in out and "rated_head_m" in out and out["shutoff_head_m"] <= out["rated_head_m"]:
            out["rated_head_m"] = out["rated_head_m"] * (1 + k)
        return out
    if isinstance(node, list):
        return [perturb(v, k) for v in node]
    return node


def perturbed_config(k: float) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="d9-perturb-"))
    shutil.copytree(CONFIG, tmp, dirs_exist_ok=True)
    if k:
        import yaml
        for path in tmp.glob("physics_*.yaml"):
            data = load_yaml(path)
            if isinstance(data, dict) and "circuits" not in data:
                path.write_text(yaml.safe_dump(perturb(data, k), allow_unicode=True, sort_keys=False), encoding="utf-8")
            elif isinstance(data, dict) and data.pop("disturbance", None) is not None:
                # 트윈 정상 흔들림 설정은 공칭 회로 민감도에 맞춘 값이라 교란 회로에서 검사에 걸린다. 정적 계산은 쓰지 않는다.
                path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return tmp


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--perturb", type=float, default=0.03)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    rng = np.random.default_rng(args.seed)
    config = perturbed_config(args.perturb)
    calcs: dict[str, PlantCalc] = {}
    offset: dict[tuple, float] = {}      # 정상점 편차(물리량 묶음별, 사례 사이 같은 공장이면 같다)
    out = []
    for cid, plant, equip, causes, expect, where, bias in CASES:
        calc = calcs.setdefault(plant, PlantCalc(plant, config=config))
        res = calc.calculate({"causes": [{"parameter": p, "value": v} for p, v in causes.items()]} if causes else {})
        control = calc.plant.control_by_key()
        units = {(e.equip_id, t.tag_id): t.unit for e in calc.plant.equipment for t in e.tags}
        values, stats = [], []
        for t in res["tags"]:
            v = t["value"]
            if v is None:
                continue
            key = (t["equip_id"], t["tag_id"])
            sigma = control[key][1] if key in control else abs(v) * 0.005
            if args.perturb:
                v = v * offset.setdefault(offset_group(calc, plant, key), 1 + rng.uniform(-0.5, 0.5) * abs(args.perturb))
            if bias and key == (bias[0], bias[1]):
                v = v * bias[2]
            latest = v + rng.normal(0, sigma)
            mean = v + rng.normal(0, sigma / np.sqrt(EFFECTIVE))
            values.append({"equip_id": key[0], "tag_id": key[1], "value": latest, "measured_at": "2026-11-26T12:00:00Z"})
            stats.append({"equip_id": key[0], "tag_id": key[1], "n": SAMPLES, "mean": mean, "std": sigma,
                          "min": mean - 3 * sigma, "max": mean + 3 * sigma, "first": v + rng.normal(0, sigma),
                          "last": latest, "first_at": "2026-11-26T11:50:00Z", "last_at": "2026-11-26T12:00:00Z"})
        out.append({"id": cid, "plant": plant, "equipment": equip, "cause": causes, "expect": expect, "location": where,
                    "sensor_bias": list(bias) if bias else None, "perturb": args.perturb,
                    "current": {"plant": plant, "as_of": "2026-11-26T12:00:00Z", "window_sec": 60, "missing": [],
                                "values": values, "stats_window_sec": 600, "stats": stats},
                    "units": [{"equipment_id": e, "tag_id": t, "unit": u} for (e, t), u in units.items()]})
    shutil.rmtree(config, ignore_errors=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"{len(out)} 사례 → {args.out} (교란 {args.perturb:+.0%})")


if __name__ == "__main__":
    main()
