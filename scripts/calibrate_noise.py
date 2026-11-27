"""입력 펌프 정상 흔들림의 원인 변동 크기를 정한다 (PU-D5a, synthetic).

실행: `python -m scripts.calibrate_noise <공장>…` — `physics_<공장>_twin.yaml` 의 disturbance 값과, 회로가 정하는 σ 를 출력한다(파일은 고치지 않는다).
- HOU·RFCC: 태그 여섯 개의 기존 σ 를 원인만으로 (1 − 계측 오차 몫)까지 설명하는 원인 크기(비음 최소제곱).
- CDU: 유량·흡입압을 설명하는 수요·흡입압. 토출압의 나머지는 두 펌프가 공유한다.
- RHDS·VRDS: 공통 회전수 변동은 HOU 와 같은 크기로 두고(2026-11-26), 유량·첫 흡입압의 σ 로 수요·흡입압을 정하며, 토출압·중간 압력의 σ 는 회로가 정한다(기존 σ 는 유량 σ 에 비해 물리적으로 작다).
"""
import sys
from itertools import combinations
from pathlib import Path

import numpy as np

from common.config_models import load_plant
from simulator.physics_adapter import PhysicsAdapter, load_physics
from simulator.physics_noise import STEPS, sensitivity, source_keys

CONFIG = Path(__file__).resolve().parent.parent / "config"
SENSOR = 0.02
ANCHORED = ("cdu", "rhds", "vrds")      # 유량·첫 흡입압만 맞추는 공장
FIXED = {"rhds": {"speed": 0.00095}, "vrds": {"speed": 0.00095}}     # 미리 정한 원인
FREE_SIGMA = ("rhds", "vrds")           # 토출압·중간 압력 σ 를 회로가 정하는 공장


def fit(key: str) -> tuple[dict[str, float], dict]:
    plant = load_plant(CONFIG / f"plant_{key}.yaml")
    a = next(x for x in load_physics(plant, CONFIG / f"physics_{key}_twin.yaml").adapters if isinstance(x, PhysicsAdapter))
    keys, control = source_keys(a), plant.control_by_key()
    sigma = np.array([control[k][1] for k in keys])
    J = {n: sensitivity(a, keys, n) for n in STEPS}
    if key in ANCHORED:
        rows = [keys.index(a.keys[a.primary][s]) for s in ("flow", "suction_pressure")]
        names = [("demand", "suction")]
    else:
        rows = list(range(len(keys)))
        names = [c for q in range(1, len(STEPS) + 1) for c in combinations(STEPS, q)]
    fixed = FIXED.get(key, {})
    target = 1 - SENSOR - sum((J[n] * x / sigma)[rows] ** 2 for n, x in fixed.items())
    best = None
    for sub in names:
        A = np.array([(J[n] / sigma)[rows] ** 2 for n in sub]).T
        scale = A.max(0)
        v = np.linalg.lstsq(A / scale, target * np.ones(len(rows)), rcond=None)[0]
        if (v < 0).any():
            continue
        explained = (A / scale) @ v
        v = v * min(1.0, 1 / (explained / target).max()) / scale
        if best is None or explained.sum() > best[0] + 1e-9:
            best = (explained.sum(), {n: float(f"{x ** 0.5:.6g}") for n, x in zip(sub, v)})
    d = best[1] | fixed
    out = {}
    if key in FREE_SIGMA:
        moved = np.sqrt(sum((J[n] * x) ** 2 for n, x in d.items()) / (1 - SENSOR))
        out = {k: float(f"{m:.6g}") for k, m in zip(keys, moved) if k not in [keys[r] for r in rows] and k[1] != a.keys[a.second]["flow"][1]}
    return d, out


if __name__ == "__main__":
    for key in sys.argv[1:]:
        print(key, *fit(key))
