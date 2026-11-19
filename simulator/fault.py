"""고장 모드(가상 시나리오) 정의와 효과 계산. 순수 계산만 하며 입출력은 파일 읽기(load_fault_modes)뿐이다.

고장 모드는 config/fault_modes.yaml 에 단계(stage)별 효과로 적는다. 효과는 태그가 아니라 신호(signal)로 지정해
같은 유형 설비에 재사용한다. 크기는 관리 기준 μ·σ 대비 z 이고, to_limit 을 주면 그 한계값 기준으로 정한다.
주입하면 설비 하나(와 propagate.driven 이면 그 설비가 구동하는 설비)의 태그마다 적용 목록(TagPlan)을 만들고,
생성기는 정상 신호 위에 효과 z 를 더한다. 멈춘 뒤에는 RECOVERY_SEC 동안 효과가 0 으로 가라앉는다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from common.config_models import ConfigError, EquipmentConfig, PlantConfig, StrictModel, TagKey, load_yaml

RECOVERY_SEC = 60       # 멈춘 뒤 효과가 0 으로 가라앉는 시간

Shape = Literal["step", "ramp", "spike", "wobble"]
LimitName = Literal["warn_hi", "alarm_hi", "trip_hi", "warn_lo", "alarm_lo", "trip_lo"]


class Effect(StrictModel):
    """한 신호의 변화. 단계 시작 + lag_sec 부터 모양대로 움직인다.
    step: 바로 z / ramp: rise_sec 동안 0→z 뒤 유지 / spike: rise_sec 동안 0→z, decay_sec 동안 z→0 / wobble: 진폭 z, 주기 period_sec 사인."""

    signal: str
    shape: Shape
    z: float | None = None              # 크기(σ 배수, 앞 효과에 더한다). to_limit 이 태그에 없을 때의 대안이기도 하다
    # 그 태그의 효과 합을 한계값까지 (margin 만큼 더: 목표 z = (한계 − μ)/σ × (1 + margin)). 앞 단계 step·ramp 가 남긴 수준을 빼고 그만큼만 움직인다
    to_limit: LimitName | None = None
    margin: float = 0.0
    lag_sec: float = Field(default=0, ge=0)
    rise_sec: float = Field(default=0, ge=0)
    decay_sec: float = Field(default=0, ge=0)
    period_sec: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _check(self) -> "Effect":
        if self.z is None and self.to_limit is None:
            raise ValueError("effect needs z or to_limit")
        if self.shape == "wobble" and self.period_sec is None:
            raise ValueError("wobble needs period_sec")
        if self.shape == "spike" and self.decay_sec <= 0:
            raise ValueError("spike needs decay_sec > 0")
        return self


class Propagation(StrictModel):
    driven: list[Effect] = []           # 축으로 이어진 아래쪽 설비(driven_by 사슬)에 주는 효과. speed 는 생성기가 구동기 값을 그대로 넘긴다


class Stage(StrictModel):
    id: str
    name: str
    at_sec: float = Field(ge=0)         # 주입 시각 기준 단계 시작
    nelson_quiet: bool = False          # 이 단계 동안은 효과를 더한 값도 규칙 1~3 을 어기지 않게 고른다 (ML 전용 전조를 만드는 합성 장치)
    effects: list[Effect] = []
    propagate: Propagation = Propagation()


class Label(StrictModel):
    """정답지 분류. 탐지·요약 입력으로 넘기지 않는다."""

    kind: str
    cause: str
    action: str
    code: Literal[0, 1] = 0     # 정답지 코드: 0 비정상(설비 고장), 1 정상(정상 운전 변화). 계측 이상은 2 (simulator.instrument)


class FaultMode(StrictModel):
    id: str
    name: str
    equip_types: list[str] = []         # 적용할 설비 유형 (예: ST)
    equip_ids: list[str] = []           # 또는 설비 ID 를 직접 (예: 원심 압축기만)
    label: Label
    stages: list[Stage]
    expect: dict[str, Any] | None = None    # 평가용 기대값 (자유 형식, 정답지에 그대로 싣는다)
    # 실제 고장이면 보일 신호지만 마스터에 태그가 없어 만들지 않는 것 (예: 증기 온도). 기록용이며 생성에는 쓰지 않는다
    unobserved: list[str] = []

    @model_validator(mode="after")
    def _check(self) -> "FaultMode":
        if not self.equip_types and not self.equip_ids:
            raise ValueError(f"fault mode {self.id}: needs equip_types or equip_ids")
        if not self.stages:
            raise ValueError(f"fault mode {self.id}: needs at least one stage")
        ats = [s.at_sec for s in self.stages]
        if ats != sorted(ats) or len(set(ats)) != len(ats):
            raise ValueError(f"fault mode {self.id}: stage at_sec must strictly increase")
        ids = [s.id for s in self.stages]
        if len(set(ids)) != len(ids):
            raise ValueError(f"fault mode {self.id}: duplicate stage id")
        return self

    def applies_to(self, e: EquipmentConfig) -> bool:
        return e.equip_id in self.equip_ids or e.type in self.equip_types


class _File(StrictModel):
    fault_modes: list[FaultMode]


def load_fault_modes(path: str | Path) -> dict[str, FaultMode]:
    """id → 고장 모드 (파일 순서)."""
    modes = _File.model_validate(load_yaml(path)).fault_modes
    ids = [m.id for m in modes]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ConfigError(f"duplicate fault mode id: {', '.join(dupes)}")
    return {m.id: m for m in modes}


# --- 주입한 사건과 태그별 적용 ---------------------------------------------------
SPEEDS = (1, 3, 5, 10)      # 진행 속도 (사용자 요청 2026-11-19, 시연용)


@dataclass(frozen=True)
class FaultEvent:
    """주입한 사건. start·stop 은 epoch 초, stop=None 은 멈추기 전.
    speed: 진행 속도. 단계 시작과 ramp 상승 시간이 1/speed 로 빨라진다. lag(물리적 응답 지연)·spike·wobble 의 모양(짧은 충격·
    진동 주기)과 멈춘 뒤 회복은 실제 시간 그대로다 — wobble 주기까지 줄이면 1초 표본에서 진동이 사라지고, spike 뒤에 이어지게
    맞춘 lag 를 줄이면 둘이 겹친다."""

    event_id: str
    equip_id: str
    mode: FaultMode
    start: float
    stop: float | None = None
    speed: int = 1


@dataclass(frozen=True)
class Applied:
    """한 태그에 걸린 효과 하나. start 는 사건 시작 기준 초(단계 시작 + lag), z 는 목표 크기."""

    stage: str
    start: float
    effect: Effect
    z: float


@dataclass(frozen=True)
class TagPlan:
    applied: tuple[Applied, ...]
    quiet: tuple[tuple[float, float], ...]      # 사건 시작 기준 [시작, 끝) — nelson_quiet 단계


def _target(effect: Effect, mu: float, sigma: float, limits, level: float) -> float | None:
    """효과의 크기 z. to_limit 이 그 태그에 있으면 효과 합이 그 한계값(+margin)에 닿도록 지금까지 남은 수준(level)을 뺀 값,
    없으면 z, 둘 다 없으면 None(이 태그에는 걸지 않는다)."""
    bound = getattr(limits, effect.to_limit) if effect.to_limit and limits is not None else None
    if bound is not None:
        return (bound - mu) / sigma * (1 + effect.margin) - level
    return effect.z


def plan(plant: PlantConfig, mode: FaultMode, equip_id: str,
         control: dict[TagKey, tuple[float, float]]) -> dict[TagKey, TagPlan]:
    """equip_id 에 mode 를 걸 때 태그별 적용 목록. 효과가 하나도 걸리지 않는 태그는 빠진다. control 은 운전점 반영 (μ, σ).
    propagate.driven 은 축으로 이어진 아래쪽 설비 모두(구동기 → 기어박스 → 압축기)에 건다."""
    driven, frontier = [], {equip_id}
    while frontier:
        nxt = [e for e in plant.equipment if e.driven_by in frontier and e not in driven and e.equip_id != equip_id]
        driven += nxt
        frontier = {e.equip_id for e in nxt}
    ends = [s.at_sec for s in mode.stages[1:]] + [math.inf]
    quiet = tuple((s.at_sec, end) for s, end in zip(mode.stages, ends) if s.nelson_quiet)
    found: dict[TagKey, list[Applied]] = {}
    for stage in mode.stages:
        targets = [(plant.equipment_by_id()[equip_id], stage.effects)] + [(e, stage.propagate.driven) for e in driven]
        for e, effects in targets:
            for effect in effects:
                if e.driven_by is not None and effect.signal == "speed":
                    continue        # 피구동 speed 는 구동기 값을 그대로 쓴다
                for t in e.tags:
                    if t.signal != effect.signal:
                        continue
                    key = (e.equip_id, t.tag_id)
                    level = sum(a.z for a in found.get(key, ()) if a.effect.shape in ("step", "ramp"))  # 앞 효과가 남긴 수준
                    z = _target(effect, *control[key], t.limits, level)
                    if z is not None:
                        found.setdefault(key, []).append(Applied(stage.id, stage.at_sec + effect.lag_sec, effect, z))
    for e in [plant.equipment_by_id()[equip_id], *driven]:
        for t in e.tags:
            if t.derive is None:
                continue
            # 계산 태그는 입력이 움직이면 따라 움직인다. 정답지에 반응 시작을 남기려고 입력의 효과를 크기 0 으로 적는다
            inherited = [Applied(a.stage, a.start, a.effect, 0.0) for i in t.derive.inputs for a in found.get((e.equip_id, i), ())]
            if inherited:
                found.setdefault((e.equip_id, t.tag_id), []).extend(inherited)
    return {key: TagPlan(tuple(a), quiet) for key, a in found.items()}


def _shape(a: Applied, tau: float, speed: int = 1) -> float:
    """효과 시작 뒤 tau 초(실제 시간)의 값 (tau ≥ 0). ramp 상승만 진행 속도로 빨라진다."""
    e = a.effect
    if e.shape == "step":
        return a.z
    if e.shape == "ramp":
        rise = e.rise_sec / speed
        return a.z if tau >= rise else a.z * tau / rise
    if e.shape == "spike":
        if tau < e.rise_sec:
            return a.z * tau / e.rise_sec
        return a.z * max(0.0, 1 - (tau - e.rise_sec) / e.decay_sec)
    return a.z * math.sin(2 * math.pi * tau / e.period_sec)


def effect_start(start_sec: float, lag_sec: float, speed: int = 1) -> float:
    """정의상 효과 시작(단계 시각 + lag)을 진행 속도에 맞춘 실제 시작(사건 시작 기준 초): 단계 시각만 1/speed."""
    return (start_sec - lag_sec) / speed + lag_sec


def tag_effect(tp: TagPlan, t: float, stop: float | None, speed: int = 1) -> tuple[float, bool]:
    """사건 시작 기준 t 초(실제 시간)의 (효과 z 합, nelson_quiet 여부). stop 도 사건 시작 기준(None 이면 멈추기 전).
    효과 시작은 단계 시각/speed + lag (effect_start). 멈추기 전에 시작하지 못한 효과는 없고,
    멈춘 뒤에는 RECOVERY_SEC 동안 선형으로 0 이 된다."""
    fade = 1.0 if stop is None or t < stop else max(0.0, 1 - (t - stop) / RECOVERY_SEC)
    if fade == 0.0:
        return 0.0, False
    z = 0.0
    for a in tp.applied:
        s = effect_start(a.start, a.effect.lag_sec, speed)
        if s <= t and (stop is None or s < stop):
            z += _shape(a, t - s, speed)
    quiet = (stop is None or t < stop) and any(lo / speed <= t < hi / speed for lo, hi in tp.quiet)
    return z * fade, quiet
