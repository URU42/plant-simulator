"""정상 흔들림의 원인 변동(흡입 경계압·회전수·수요)을 회로 민감도로 입력 펌프 두 대의 유량·압력에 전달한다 (PU-D5a, synthetic).

태그 값의 흔들림 z = L·w + r·e. w 는 회로가 공유하는 원인(표준정규 AR), e 는 태그별 계측 오차다. L 은 정상 운전점의 선형 민감도 ×
원인 크기 / σ 이고 r 은 분산 합이 1 이 되게 정한다. 후속 펌프는 사이의 탑·드럼 모델이 없어 쓰지 않는다(기존 태그별 잡음).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

import numpy as np
from pydantic import Field

from common.config_models import ConfigError, StrictModel, TagKey
from simulator.physics.station import UnitState

if TYPE_CHECKING:
    from simulator.physics_adapter import PhysicsAdapter

Relative = Annotated[float, Field(ge=0, lt=0.2)]
STEPS = {"suction": 1e-3, "speed": 2e-4, "speed_second": 2e-4, "demand": 1e-3}


class Disturbance(StrictModel):
    """원인마다 정상값 대비 표준편차(비). speed 는 두 대 공통(전원 주파수), speed_second 는 둘째 펌프만, demand 는 하류 분기 저항."""
    suction: Relative = 0.0
    speed: Relative = 0.0
    speed_second: Relative = 0.0
    demand: Relative = 0.0
    sensor_variance: float = Field(default=0.02, gt=0, lt=1)       # 태그별 계측 오차의 분산 몫
    shared_header: bool = False     # 병렬: 원인이 설명하지 못한 토출압 변동을 두 펌프가 공유한다(같은 헤더)


def solve_disturbed(a: "PhysicsAdapter", change: dict[str, float]) -> dict[TagKey, float]:
    """원인을 change 만큼(비) 움직인 정상 회로의 태그 중심값."""
    case = a.case
    demand = 1 + change.get("demand", 0.0)
    branches = [b.model_copy(update={"pipes": {n: p.model_copy(update={"minor_loss_k": p.minor_loss_k * demand})
                                              for n, p in b.pipes.items()}}) for b in case.network.branches]
    station = case.supply().model_copy(update={"demand": case.network.model_copy(update={"branches": branches})})
    point = case.default_point.model_copy(update={"suction_abs_pa": case.default_point.suction_abs_pa * (1 + change.get("suction", 0.0))})
    speed = 1 + change.get("speed", 0.0)
    states = {case.equip_id: UnitState(speed_ratio=speed), case.second: UnitState(speed_ratio=speed * (1 + change.get("speed_second", 0.0)))}
    return a.sensor_centers(case.result(point, station.solve(point, states)))


def source_keys(a: "PhysicsAdapter") -> list[TagKey]:
    """원인 변동을 받는 태그: 입력 펌프 두 대의 흡입압·토출압·유량 (전류는 그 값의 계산 태그)."""
    return [a.keys[e][s] for e in (a.primary, a.second) for s in ("suction_pressure", "discharge_pressure", "flow")]


def sensitivity(a: "PhysicsAdapter", keys: list[TagKey], name: str) -> np.ndarray:
    h = STEPS[name]
    up, down = solve_disturbed(a, {name: h}), solve_disturbed(a, {name: -h})
    return np.array([(up[k] - down[k]) / (2 * h) for k in keys])


def loadings(a: "PhysicsAdapter", d: Disturbance) -> tuple[list[TagKey], np.ndarray, np.ndarray]:
    """(태그, L, r). 원인이 만드는 흔들림이 태그 σ 를 넘으면 설정 오류다."""
    keys = source_keys(a)
    sigma = np.array([a.control[k][1] for k in keys])
    L = np.column_stack([sensitivity(a, keys, n) * getattr(d, n) / sigma for n in STEPS])
    rest = 1 - (L * L).sum(1)
    if d.shared_header:
        header = np.zeros(len(keys))
        for e in (a.primary, a.second):
            i = keys.index(a.keys[e]["discharge_pressure"])
            header[i] = np.sqrt(max(0.0, rest[i] - d.sensor_variance))
        L = np.column_stack([L, header])
        rest = 1 - (L * L).sum(1)
    if rest.min() < 0.5 * d.sensor_variance:
        raise ConfigError(f"{a.primary}: 원인 변동이 {keys[int(rest.argmin())]} 의 σ 보다 큽니다.")
    return keys, L, np.sqrt(rest)
