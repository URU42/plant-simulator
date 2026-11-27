"""브라우저 E2E 채점용 기대값을 Git 제외 경로에 쓴다 (평가 전용, 서비스·AI 입력 아님).

플랜트마다 정상 해와 시나리오별 최대 변화의 물리 태그 중심값, 관리 기준 σ, 예상 한계 교차 태그를 계산한다.
"""
import argparse
import json
from pathlib import Path

from common.config_models import load_plant
from simulator.physics_adapter import PhysicsBundle, load_physics
from simulator.physics_single import SingleAdapter

ROOT = Path(__file__).resolve().parents[1]
PLANTS = ("cdu", "hou", "rfcc", "rhds", "vrds")
# (역할, 모드) → 회로 solve 인자. 결합 회로: (첫 입력 스트레이너, 마모, 밸브, 후속 스트레이너, 마모, 둘째 입력 스트레이너, 마모)
COUPLED = {
    ("source", "PM-SUCTION-LOSS"): (1, 0, 0, 0, 0), ("source", "PM-IMPELLER-WEAR"): (0, 1, 0, 0, 0),
    ("source", "PM-DISCHARGE-VALVE-RESTRICTION"): (0, 0, 1, 0, 0), ("source", "OP-THROUGHPUT-CHANGE"): (0, 0, 0.2, 0, 0),
    ("downstream", "PM-SUCTION-LOSS"): (0, 0, 0, 1, 0), ("downstream", "PM-IMPELLER-WEAR"): (0, 0, 0, 0, 1),
    ("second", "PM-SUCTION-LOSS"): (0, 0, 0, 0, 0, 1, 0), ("second", "PM-IMPELLER-WEAR"): (0, 0, 0, 0, 0, 0, 1),
}
# 단일 회로: (스트레이너, 마모, 흡입 온도 상승). PM-SUCTION-HOT 은 S1 온도 1.0 + S3 막힘 0.55.
SINGLE = {"PM-SUCTION-LOSS": (1, 0, 0), "PM-SUCTION-HOT": (0.55, 0, 1), "PM-IMPELLER-WEAR": (0, 1, 0),
          "FN-IMPELLER-FOULING": (0, 1, 0), "FN-INLET-RESTRICTION": (1, 0, 0)}
IMPACT_SIGMA = 3.0      # 화면의 연결 설비 영향 판정(2.5σ)보다 여유를 둔 채점 기준
DEVIATION_MARGIN = 0.5  # 편차 기준(4σ) 근처는 잡음으로 흔들리므로 채점하지 않는다


def physics_file(key: str) -> Path:
    return ROOT / f"config/physics_{key}_twin.yaml"


def breach(value: float, limits: dict) -> str | None:
    for level in ("trip", "alarm", "warn"):
        if f"{level}_lo" in limits and value < limits[f"{level}_lo"]:
            return level
        if f"{level}_hi" in limits and value > limits[f"{level}_hi"]:
            return level
    return None


def build(key: str) -> dict:
    plant = load_plant(ROOT / f"config/plant_{key}.yaml")
    physics = load_physics(plant, physics_file(key))
    adapters = physics.adapters if isinstance(physics, PhysicsBundle) else [physics]
    control = plant.control_by_key()
    limits = {(e.equip_id, t.tag_id): (t.limits.model_dump(exclude_none=True) if t.limits else {})
              for e in plant.equipment for t in e.tags}
    name = lambda k: f"{k[0]}/{k[1]}"
    normal = {}
    for adapter in adapters:
        normal.update(adapter.sensor_centers(adapter.solve(0, 0, 0)))
    scenarios = []
    for adapter in adapters:
        single = isinstance(adapter, SingleAdapter)
        modes = ([((adapter.primary, m), a) for m, a in SINGLE.items()] if single else
                 [(({"source": adapter.primary, "second": adapter.second, "downstream": adapter.downstream}[r], m), a)
                  for (r, m), a in COUPLED.items()])
        catalog = {(e.equip_id, m.id) for e in plant.equipment for m in physics_modes(plant, physics, e.equip_id)}
        for (equip, mode), args in modes:
            if (equip, mode) not in catalog:
                continue
            centers = adapter.sensor_centers(adapter.solve(*args))
            z = {k: (v - normal[k]) / control[k][1] for k, v in centers.items()}
            breached = {k[0] for k, v in centers.items() if breach(v, limits[k])}
            # 화면 규칙과 같다: 기준 설비 = 한계 경보 설비, 없으면 편차(≥4σ)가 가장 큰 설비.
            anchors, robust = set(breached), True
            if not anchors:
                peak = {}
                for k, value in z.items():
                    peak[k[0]] = max(peak.get(k[0], 0.0), abs(value))
                ranked = sorted(peak.items(), key=lambda item: -item[1])
                if ranked and ranked[0][1] >= 4 + DEVIATION_MARGIN:
                    anchors = {ranked[0][0]}
                    robust = len(ranked) == 1 or ranked[0][1] >= 1.5 * ranked[1][1]
                elif ranked and ranked[0][1] >= 4 - DEVIATION_MARGIN:
                    robust = False
            others = {k: v for k, v in z.items() if anchors and k[0] not in anchors and not breach(centers[k], limits[k])}
            impacts = sorted(name(k) for k, v in others.items() if abs(v) >= IMPACT_SIGMA)
            # 채점: 3σ 이상은 표시되어야 하고, 기준 설비가 없거나 다른 설비 변화가 모두 1.5σ 미만이면 표시되면 안 된다.
            no_impact = not anchors or all(abs(v) < 1.5 for v in others.values())
            scenarios.append({
                "equip_id": equip, "circuit_id": adapter.circuit_of(equip), "mode": mode,
                "centers": {name(k): v for k, v in centers.items()},
                "breaches": {name(k): b for k, v in centers.items() if (b := breach(v, limits[k]))},
                "impacts": impacts if robust else [], "no_impact": no_impact and robust,
                "anchors": sorted(anchors), "anchor_robust": robust,
            })
    return {"plant": key.upper(), "circuits": [c["id"] for a in adapters for c in a.circuits()],
            "normal": {name(k): v for k, v in normal.items()},
            "sigma": {name(k): control[k][1] for k in normal}, "scenarios": scenarios}


def physics_modes(plant, physics, equip_id):
    from collector.anomaly_book import AnomalyBook
    from collector.seed_master import seed_master
    from collector.storage import Storage
    from simulator.fault import load_fault_modes
    storage = Storage(":memory:")
    try:
        seed_master(storage, plant)
        book = AnomalyBook(storage, plant, load_fault_modes(ROOT / "config/fault_modes.yaml"), physics=physics)
        return [m for m in book.applicable(equip_id) if physics.physical_parameters(m, equip_id)]
    finally:
        storage.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help=".cache/d4 아래 새 JSON")
    parser.add_argument("--plants", default=",".join(PLANTS))
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to(ROOT / ".cache"):
        parser.error("평가 기대값은 Git 제외 .cache 아래에만 쓴다.")
    with args.output.open("x", encoding="utf-8") as out:
        json.dump({key.upper(): build(key) for key in args.plants.split(",")}, out, indent=2, ensure_ascii=False)
    print(args.output)


if __name__ == "__main__":
    main()
