"""이상 사건의 모양과 반응 태그, 분류 (data-v3 의 build_dataset.py·signal_engine.cpp 방식).

사건은 설비 하나에서 규칙 하나를 일으킨다. 설비의 대표 진동 태그가 사건 규칙대로 움직이고, 규칙에 따라 다른 태그가
시차를 두고 따라 움직인다. 모양은 z(관리 기준 μ·σ 대비 편차) 템플릿이며 규칙 3 만 주기 16 의 톱니(data-v3 와 다른 점)다.
순수 계산만 하며 입출력은 없다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Literal

from common.config_models import PlantConfig

Origin = Literal["spontaneous", "persistent", "injected"]

SAWTOOTH = 16           # 규칙 3 톱니의 주기(표본 수). 극단 사이 위반 간격이 판정 묶음의 닫힘(15 표본) 안에 들도록
MAX_RECOVERY_SEC = 60   # 반응 표의 가장 긴 회복 시간

VIBRATION = ("vibration", "shaft_vibration")
THERMAL = ("bearing_temp", "oil_temp")
LUBE_SIDE = VIBRATION + ("current", "oil_pressure")
CONTROL_SIDE = VIBRATION + ("suction_pressure", "discharge_pressure", "flow", "current", "differential_pressure")
NEGATIVE = ("oil_pressure", "flow", "suction_pressure")      # 사건 때 값이 내려가는 신호


@dataclass(frozen=True)
class AnomalyEvent:
    """start·end 는 epoch 초. end=None 은 멈춤 전까지."""

    event_id: str
    origin: Origin
    equip_id: str
    rule: int
    start: float
    end: float | None
    normal_change: bool = False


@dataclass(frozen=True)
class Response:
    """한 태그의 반응: 따르는 규칙, 방향(±1), 시작 지연(초), 사건이 끝난 뒤 가라앉는 시간(초)."""

    rule: int
    direction: float
    lag_sec: int
    recovery_sec: int


def primary_tag(plant: PlantConfig, equip_id: str) -> str:
    """설비의 대표 태그: 설정 순서상 첫 진동 태그."""
    for t in plant.equipment_by_id()[equip_id].tags:
        if t.signal in VIBRATION:
            return t.tag_id
    raise ValueError(f"{equip_id}: no vibration tag")


def responses(plant: PlantConfig, equip_id: str, rule: int) -> dict[str, Response]:
    """사건 규칙에 반응하는 태그와 반응 (tag_id → Response). 설정 순서대로."""
    primary = primary_tag(plant, equip_id)
    out: dict[str, Response] = {}
    for t in plant.equipment_by_id()[equip_id].tags:
        d = -1.0 if t.signal in NEGATIVE else 1.0
        if t.tag_id == primary:
            out[t.tag_id] = Response(rule, 1.0, 0, 0 if rule == 7 else 5)
        elif rule == 7 or t.signal == "speed":
            continue
        elif rule in (4, 8):
            if t.signal in CONTROL_SIDE:
                out[t.tag_id] = Response(rule, d, 2, 15)
        elif t.signal in THERMAL:
            out[t.tag_id] = Response(3 if rule == 3 else 2, 1.0, 40, 60)
        elif t.signal in LUBE_SIDE:
            out[t.tag_id] = Response(rule, d, 5, 10)
    return out


def template_z(rule: int, k: int, u: float) -> float:
    """규칙의 모양. k 는 반응 시작 후 표본 번호, u 는 [-1, 1] 잡음."""
    if rule == 1:
        return 3.5 + 0.12 * u
    if rule == 2:
        return 0.65 + 0.18 * u
    if rule == 3:
        return -2.4 + 4.8 * (k % SAWTOOTH) / (SAWTOOTH - 1) + u / 20
    if rule == 4:
        return (1 if k % 2 else -1) * (0.7 + 0.12 * u)
    if rule == 5:
        return -0.25 if k % 3 == 2 else 2.35 + 0.15 * u
    if rule == 6:
        return -0.3 if k % 5 == 4 else 1.55 + 0.17 * u
    if rule == 7:
        return 0.025
    if rule == 8:
        return (1 if k % 4 < 2 else -1) * (1.55 + 0.18 * u)
    raise ValueError(f"rule {rule}")


def tag_z(event: AnomalyEvent, resp: Response, t: float, interval: int, u: float) -> float | None:
    """시각 t 의 태그 z 편차. 반응 구간(지연 전, 회복 뒤) 밖이면 None."""
    begin = event.start + resp.lag_sec
    if t < begin:
        return None
    z = template_z(resp.rule, int((t - begin) // interval), u) * resp.direction
    if event.end is None or t < event.end:
        return z
    if resp.recovery_sec <= 0:
        return None
    f = (event.end + resp.recovery_sec - t) / resp.recovery_sec     # 끝난 뒤 1 → 0 으로 가라앉는 비율
    return None if f <= 0 else z * f + 0.08 * u


def classify(rule: int, normal_change: bool) -> tuple[str, str, str]:
    """(종류, 원인, 조치)."""
    if normal_change:
        return "정상 운전 변화", "의도한 운전 부하 변경", "부하 조건 복귀; 기계 수리 아님"
    if rule == 7:
        return "계측 이상", "대표 진동 센서 값 고착", "센서·신호 변환기 점검 및 복구"
    if rule == 8:
        return "계측·제어 이상", "신호원 혼입 또는 제어계통의 두 상태 왕복", "신호원·제어계통 점검 및 복구"
    if rule == 4:
        return "기계·제어 이상", "유량·압력 제어 헌팅", "제어 밸브·제어기 점검 및 복구"
    return "기계 이상", "베어링·윤활 계통의 간헐적 성능 저하", "윤활 공급·기계 상태 점검 및 복구"


def resolve(events: list[AnomalyEvent], stops: dict[str, float]) -> list[AnomalyEvent]:
    """멈춤(event_id → 시각)을 반영하고, 같은 설비의 주입·지속 고장과 겹치는 자동 이상을 자른다.
    자동 이상은 시작 때 이미 고장이 살아 있으면 빼고, 도중에 고장이 시작하면 그 시작에서 끝낸다."""
    stopped = [replace(e, end=min(e.end if e.end is not None else math.inf, stops[e.event_id]))
               if e.event_id in stops else e for e in events]
    stopped = [e for e in stopped if e.end is None or e.end > e.start]     # 시작 전에 멈춘 사건은 일어난 적 없다
    faults = [e for e in stopped if e.origin != "spontaneous"]
    out: list[AnomalyEvent] = []
    for e in stopped:
        if e.origin == "spontaneous":
            mine = [f for f in faults if f.equip_id == e.equip_id]
            if any(f.start <= e.start and (f.end is None or e.start < f.end) for f in mine):
                continue
            cut = min((f.start for f in mine if e.start < f.start and (e.end is None or f.start < e.end)), default=None)
            if cut is not None:
                e = replace(e, end=cut)
        out.append(e)
    return out
