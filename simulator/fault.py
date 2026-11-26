"""고장 모드(가상 시나리오) 정의와 효과 계산. 순수 계산만 하며 입출력은 파일 읽기(load_fault_modes)뿐이다.

고장 모드는 config/fault_modes.yaml 에 단계(stage)별 효과로 적는다. 효과는 태그가 아니라 신호(signal)로 지정해
같은 유형 설비에 재사용한다. 크기는 관리 기준 μ·σ 대비 z 이고, to_limit 을 주면 그 한계값 기준으로 정한다.
주입하면 설비 하나(와 propagate.driven 이면 그 설비가 구동하는 설비, propagate.process·utility 면 공정 흐름 그래프의 하류 설비)의
태그마다 적용 목록(TagPlan)을 만들고, 생성기는 정상 신호 위에 효과 z 를 더한다. 멈춘 뒤에는 회복 시간(recovery_sec: 온도는 느리다. 하류 k단계는 × (k+1))
동안 효과가 0 으로 가라앉는다. 손상 단계(damage)의 step·ramp 효과는 멈춘 시각의 크기로 남아 있다가 수리(repair) 뒤
같은 회복 시간 동안 0 으로 가라앉는다. 고장 모드의 boost 는 같은 설비의 다른 신호(예: 유체 온도)가 사건으로 오른 만큼(태그 단위)
효과를 키우는 교호작용이다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from common.config_models import ConfigError, EquipmentConfig, PlantConfig, StrictModel, TagKey, load_yaml

RECOVERY_SEC = 60       # 멈춘 뒤 효과가 0 으로 가라앉는 시간 (유량·압력·진동 등 빠른 신호)
TEMP_RECOVERY_SEC = 300 # 온도 신호의 회복 시간 (열용량으로 느리다. 수치는 synthetic, 2026-11-25 D2)
# 공정 흐름 하류 전파 (완만한 감쇠, 사용자 확정 2026-11-20, 수치는 synthetic): k단계 크기 ×0.7^(k−1), lag·rise ×k, 회복 ×(k+1)
MAX_HOP = 2
HOP_RATIO = 0.7
MIN_HOP_Z = 0.25        # 줄인 뒤 |z| 가 이 이하면 걸지 않는다
MAX_RECOVERY_SEC = TEMP_RECOVERY_SEC * (MAX_HOP + 1)     # 사건 효과가 남아 있을 수 있는 최대 시간 (멈춤 뒤)


def recovery_sec(signal: str) -> int:
    """신호(또는 물리 파라미터) 이름의 회복 시간. 온도는 느리다."""
    return TEMP_RECOVERY_SEC if "temp" in signal else RECOVERY_SEC


Shape = Literal["step", "ramp", "spike", "wobble"]
HELD = ("step", "ramp")     # 손상 단계에서 멈춘 뒤에도 남는 모양 (spike·wobble 은 순간 현상이라 남지 않는다)
LimitName = Literal["warn_hi", "alarm_hi", "trip_hi", "warn_lo", "alarm_lo", "trip_lo"]


class Effect(StrictModel):
    """한 신호의 변화. 단계 시작 + lag_sec 부터 모양대로 움직인다.
    step: 바로 z / ramp: rise_sec 동안 0→z 뒤 유지 / spike: rise_sec 동안 0→z, decay_sec 동안 z→0 / wobble: 진폭 z, 주기 period_sec 사인."""

    signal: str
    shape: Shape
    z: float | None = None              # 크기(σ 배수, 앞 효과에 더한다). to_limit 이 태그에 없을 때의 대안이기도 하다
    delta: float | None = None          # 크기(태그 단위, 예: +15 ℃). σ 가 설비마다 달라도 같은 물리량만큼 움직인다. z 보다 먼저 쓴다
    # 그 태그의 효과 합을 한계값까지 (margin 만큼 더: 목표 z = (한계 − μ)/σ × (1 + margin)). 앞 단계 step·ramp 가 남긴 수준을 빼고 그만큼만 움직인다
    to_limit: LimitName | None = None
    margin: float = 0.0
    lag_sec: float = Field(default=0, ge=0)
    rise_sec: float = Field(default=0, ge=0)
    decay_sec: float = Field(default=0, ge=0)
    period_sec: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _check(self) -> "Effect":
        if self.z is None and self.to_limit is None and self.delta is None:
            raise ValueError("effect needs z, delta or to_limit")
        if self.shape == "wobble" and self.period_sec is None:
            raise ValueError("wobble needs period_sec")
        if self.shape == "spike" and self.decay_sec <= 0:
            raise ValueError("spike needs decay_sec > 0")
        return self


class Propagation(StrictModel):
    driven: list[Effect] = []           # 축으로 이어진 아래쪽 설비(driven_by 사슬)에 주는 효과. speed 는 생성기가 구동기 값을 그대로 넘긴다
    process: list[Effect] = []          # 공정 흐름으로 닿은 하류 설비 (1단계 기준. 단계마다 줄인다)
    utility: list[Effect] = []          # 보조 계통 간선으로 바로 닿은 설비 (윤활유·보충 수소). 거기서 멈춘다. to_limit 가능

    @model_validator(mode="after")
    def _downstream(self) -> "Propagation":
        for e in self.process + self.utility:
            if e.z is None or e.shape not in ("ramp", "wobble"):
                raise ValueError("process/utility effects need z and shape ramp or wobble")
        # 공정 하류는 완만한 감쇠(σ 배수)만, 보조 계통은 한계 목표도 허용 (윤활유 상실 → 압축기 베어링, 사용자 확정 2026-11-23)
        if any(e.to_limit is not None for e in self.process):
            raise ValueError("process effects cannot use to_limit")
        return self


# discharge_valve_restriction: 펌프 자체 토출 차단밸브(정적 계산 전용 모드 config/fault_modes_calc.yaml). 트윈 회로는 쓰지 않는다.
HydraulicParameter = Literal["strainer_blockage", "impeller_wear", "valve_restriction", "suction_temperature_rise",
                             "discharge_valve_restriction"]


class HydraulicEffect(StrictModel):
    parameter: HydraulicParameter
    amount: float = Field(gt=0, le=1)
    rise_sec: float = Field(gt=0, allow_inf_nan=False)


class Stage(StrictModel):
    id: str
    name: str
    at_sec: float = Field(ge=0)         # 주입 시각 기준 단계 시작
    nelson_quiet: bool = False          # 이 단계 동안은 효과를 더한 값도 규칙 1~3 을 어기지 않게 고른다 (ML 전용 전조를 만드는 합성 장치)
    damage: bool = False                # 손상 단계: step·ramp 효과가 멈춘 뒤에도 수리 전까지 남는다 (대상 단계 사용자 확정 2026-11-23)
    effects: list[Effect] = []
    propagate: Propagation = Propagation()
    hydraulic: list[HydraulicEffect] = []


class Label(StrictModel):
    """정답지 분류. 탐지·요약 입력으로 넘기지 않는다."""

    kind: str
    cause: str
    action: str
    code: Literal[0, 1] = 0     # 정답지 코드: 0 비정상(설비 고장), 1 정상(정상 운전 변화). 계측 이상은 2 (simulator.instrument)


class Factor(StrictModel):
    """요인 곱 교호작용의 요인 하나. 수준 = clamp(direction × 변화 / span, 0, 1).
    변화는 사건의 step·ramp 효과만 본다 (맥동이 자기 수준을 되먹이지 않게. 정상 잡음·계측 이상 제외).
    over 가 없으면 그 태그의 효과 z, 있으면 (signal + offset)/(over + offset) 비의 운전점 대비 상대 변화다 (예: 절대압 압축비)."""

    signal: str
    over: str | None = None
    offset: float = 0.0                 # 게이지압 → 절대압 (over 가 있을 때)
    direction: Literal[-1, 1]
    span: float = Field(gt=0)


class Boost(StrictModel):
    """교호작용 (2026-11-23 사용자 확정, 계수는 synthetic): 같은 설비 by 신호 태그의 상태에 따라 signals 효과를 키운다. 세 방식 중 하나.
    - per_unit (선형): 배율 = min(max_gain, 1 + per_unit × max(0, ΔT)). ΔT 는 by 태그가 사건으로 오른 만큼(태그 단위, 정상 잡음·계측
      이상 제외). 내려가면 키우지 않는다. 예: 흡입 유체 온도 → 캐비테이션.
    - exponent (비율 거듭제곱): 배율 = min(max_gain, (N / N_설계)^exponent). N = 운전점 μ + 사건 효과(태그 단위). 낮으면 1 보다 작다.
      키우는 태그의 효과 크기(to_limit 포함)는 설계 운전점 μ 로 정한다 — 운전점이 옮긴 μ 로 정하면 한계 목표가 회전수 차이를 상쇄한다.
      예: 팬 회전수 → 불균형 진동 (불균형 힘 ∝ 회전수²).
    - factors (요인 곱, 2026-11-25): 배율 = min(max_gain, 1 + gain × 요인 수준의 곱). by 대신 factors 를 쓴다. 요인이 하나라도 0 이면 1.
      멈춘 뒤에는 멈춘 시각의 배율을 유지한다 (손상이 남는 동안 요인이 회복해도 손상 크기는 그대로)."""

    rule: Literal["I3", "I4", "I5", "I6"] | None = None     # 교호작용 규칙 번호 (정답지·대조 실험)
    by: str | None = None               # 예: process_temp (흡입 유체 온도), speed (회전수)
    signals: list[str]                  # 키울 효과의 신호 (예: vibration, flow)
    per_unit: float | None = Field(default=None, gt=0)    # 선형: by 태그 단위 1 당 배율 증가 (예: ℃ 당 0.03)
    exponent: float | None = Field(default=None, gt=0)    # 비율 거듭제곱의 지수 (예: 2)
    factors: list[Factor] = []
    gain: float | None = Field(default=None, gt=0)        # 요인 곱의 이득 (factors 와 함께)
    shapes: list[Shape] = []            # 요인 곱이 키울 효과 모양 (비우면 전부). 요인이면서 반응인 신호의 ramp 를 다시 키우지 않게 좁힌다
    max_gain: float = Field(gt=1)
    # 적용할 설비 (비우면 by 태그가 있는 모든 설비). by 태그의 뜻이 설비마다 다를 때 좁힌다
    # (예: process_temp 가 흡입 유체 온도인 펌프만 — 2026-11-23 사용자 확정)
    equip_ids: list[str] = []
    # 경고 구간 보장: 그 태그의 경보 한계 목표(to_limit alarm_*) 효과가 시작되기 전에는 키운 효과가 경보 한계 HOLD_MARGIN_Z 안쪽에 머문다.
    # 경고와 경보가 거의 같이 나지 않게 경고 단계를 남긴다. 경보 단계부터는 그대로 키운다 (사용자 확정 2026-11-23)
    hold_below_alarm: bool = False

    @model_validator(mode="after")
    def _one_kind(self) -> "Boost":
        if sum((self.per_unit is not None, self.exponent is not None, bool(self.factors))) != 1:
            raise ValueError("boost needs exactly one of per_unit, exponent or factors")
        if bool(self.factors) != (self.gain is not None) or bool(self.factors) == (self.by is not None):
            raise ValueError("boost needs factors with gain, or by")
        if self.factors and (len(self.factors) < 2 or self.hold_below_alarm) or self.shapes and not self.factors:
            raise ValueError("boost factors need at least two and no hold_below_alarm; shapes need factors")
        return self


class FaultMode(StrictModel):
    id: str
    name: str
    equip_types: list[str] = []         # 적용할 설비 유형 (예: ST)
    equip_ids: list[str] = []           # 또는 설비 ID 를 직접 (예: 원심 압축기만)
    label: Label
    basis: str | None = None            # 가정 기반 표시 (물리 계산 없이 정한 효과). 있으면 트윈에서도 주입할 수 있다. 정답 분류가 아니다
    stages: list[Stage]
    expect: dict[str, Any] | None = None    # 평가용 기대값 (자유 형식, 정답지에 그대로 싣는다)
    # 실제 고장이면 보일 신호지만 마스터에 태그가 없어 만들지 않는 것 (예: 증기 온도). 기록용이며 생성에는 쓰지 않는다
    unobserved: list[str] = []
    physics_only: bool = False
    boost: Boost | None = None          # 교호작용 (주입 설비의 효과만 키운다. 하류·축 전파 효과는 그대로)

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
    repair: float | None = None         # 수리 시각 (epoch 초). 손상 효과는 이때부터 회복한다


@dataclass(frozen=True)
class Applied:
    """한 태그에 걸린 효과 하나. start 는 사건 시작 기준 초(단계 시작 + lag), z 는 목표 크기."""

    stage: str
    start: float
    effect: Effect
    z: float
    damage: bool = False                # 손상 단계의 step·ramp: 멈춘 뒤에도 수리 전까지 남는다


HOLD_MARGIN_Z = 3.0         # 경고 구간 보장: 키운 효과의 상한 = 경보 한계 z − 3 (잡음으로 경보를 건드리지 않게)


@dataclass(frozen=True)
class FactorRef:
    """한 설비에 묶은 요인: 태그 키와 운전점 (μ + offset, σ). over 는 비의 분모."""

    factor: Factor
    key: TagKey
    point: tuple[float, float]
    over: tuple[TagKey, float, float] | None = None

    def level(self, z_of) -> float:
        change = z_of(self.key)
        if self.over is not None:
            (mu, sg), (key2, mu2, sg2) = self.point, self.over
            change = (mu + sg * change) / mu * mu2 / (mu2 + sg2 * z_of(key2)) - 1
        return min(1.0, max(0.0, self.factor.direction * change / self.factor.span))


@dataclass(frozen=True)
class TagBoost:
    """한 태그에 걸린 교호작용. 선형이면 per_z 는 by 태그 z 1 당 배율 증가(= per_unit × σ_by).
    비율 거듭제곱이면 power = (운전점 μ_by / 설계 μ_by, σ_by / 설계 μ_by, 지수) 이고 per_z 는 쓰지 않는다.
    hold 는 경고 구간 보장: (경보 목표 효과의 정의상 시작 초, 그 lag, 그 rise_sec, |z| 상한). 그 시작 전에는 키운 효과가 상한을 넘지 않고,
    시작 뒤 rise_sec 동안 상한을 서서히 푼다 (한꺼번에 풀면 단계 경계에서 값이 계단처럼 튄다)."""

    by: TagKey | None
    per_z: float
    max_gain: float
    hold: tuple[float, float, float, float] | None = None
    power: tuple[float, float, float] | None = None
    factors: tuple[FactorRef, ...] = ()
    gain: float = 0.0
    shapes: tuple[Shape, ...] = ()
    only: bool = False          # 키운 만큼((배율 − 1) × 효과)만 낸다. 원래 효과는 물리 회로 해가 이미 낸다

    def by_tags(self) -> str:
        """정답지에 남길 요인 태그 ID (요인 곱이면 쉼표로 잇는다)."""
        return self.by[1] if self.by else ",".join(f.key[1] for f in self.factors)


@dataclass(frozen=True)
class TagPlan:
    applied: tuple[Applied, ...]
    quiet: tuple[tuple[float, float], ...]      # 사건 시작 기준 [시작, 끝) — nelson_quiet 단계
    via: str = "self"                           # self(고장 설비) · driven(축) · process · utility (정답지)
    hop: int = 0                                # 흐름 그래프 단계 (고장 설비와 축 사슬은 0)
    recovery: float = RECOVERY_SEC              # 멈춘 뒤 0 으로 가라앉는 시간
    boost: TagBoost | None = None               # 교호작용 (이 태그에 걸린 것)
    extra: "TagPlan | None" = None              # 회로 해(via physics) 위에 더하는 교호작용 몫


def _hop_effect(effect: Effect, hop: int) -> Effect | None:
    """하류 k단계 효과: 크기 ×0.7^(k−1), lag·rise ×k (늦고 천천히). 줄인 크기가 MIN_HOP_Z 이하면 None."""
    z = effect.z * HOP_RATIO ** (hop - 1)
    if abs(z) <= MIN_HOP_Z:
        return None
    return effect.model_copy(update={"z": z, "lag_sec": effect.lag_sec * hop, "rise_sec": effect.rise_sec * hop})


def _target(effect: Effect, mu: float, sigma: float, limits, level: float) -> float | None:
    """효과의 크기 z. to_limit 이 그 태그에 있으면 효과 합이 그 한계값(+margin)에 닿도록 지금까지 남은 수준(level)을 뺀 값,
    없으면 delta/σ 나 z, 모두 없으면 None(이 태그에는 걸지 않는다)."""
    bound = getattr(limits, effect.to_limit) if effect.to_limit and limits is not None else None
    if bound is not None:
        return (bound - mu) / sigma * (1 + effect.margin) - level
    return effect.delta / sigma if effect.delta is not None else effect.z


def plan(plant: PlantConfig, mode: FaultMode, equip_id: str,
         control: dict[TagKey, tuple[float, float]]) -> dict[TagKey, TagPlan]:
    """equip_id 에 mode 를 걸 때 태그별 적용 목록. 효과가 하나도 걸리지 않는 태그는 빠진다. control 은 운전점 반영 (μ, σ).
    propagate.driven 은 축으로 이어진 아래쪽 설비 모두(구동기 → 기어박스 → 압축기)에 건다.
    propagate.process·utility 는 고장 설비와 축 사슬에서 흐름 그래프로 닿는 설비(PlantConfig.downstream)에 단계만큼 줄여 건다."""
    by_id = plant.equipment_by_id()
    driven, frontier = [], {equip_id}
    while frontier:
        nxt = [e for e in plant.equipment if e.driven_by in frontier and e not in driven and e.equip_id != equip_id]
        driven += nxt
        frontier = {e.equip_id for e in nxt}
    role = {equip_id: ("self", 0)} | {e.equip_id: ("driven", 0) for e in driven}
    role |= {d: (via, hop) for d, (hop, via) in plant.downstream(list(role), MAX_HOP).items()}
    ends = [s.at_sec for s in mode.stages[1:]] + [math.inf]
    quiet = tuple((s.at_sec, end) for s, end in zip(mode.stages, ends) if s.nelson_quiet)
    found: dict[TagKey, list[Applied]] = {}
    # 비율 거듭제곱 교호작용으로 키우는 태그는 효과 크기를 설계 운전점 μ 로 정한다 (Boost 설명)
    design = {(equip_id, t.tag_id): (t.control.mean, t.control.sigma) for t in by_id[equip_id].tags
              if mode.boost and mode.boost.exponent and t.signal in mode.boost.signals}
    for stage in mode.stages:
        effects_of = {"self": stage.effects, "driven": stage.propagate.driven, "process": stage.propagate.process,
                      "utility": stage.propagate.utility}
        for d, (via, hop) in role.items():
            e = by_id[d]
            effects = [x for x in (_hop_effect(f, hop) if hop else f for f in effects_of[via]) if x is not None]
            for effect in effects:
                if e.driven_by is not None and effect.signal == "speed":
                    continue        # 피구동 speed 는 구동기 값을 그대로 쓴다
                for t in e.tags:
                    if t.signal != effect.signal:
                        continue
                    key = (e.equip_id, t.tag_id)
                    level = sum(a.z for a in found.get(key, ()) if a.effect.shape in ("step", "ramp"))  # 앞 효과가 남긴 수준
                    z = _target(effect, *design.get(key, control[key]), t.limits, level)
                    if z is not None:
                        held = stage.damage and effect.shape in HELD
                        found.setdefault(key, []).append(Applied(stage.id, stage.at_sec + effect.lag_sec, effect, z, held))
    for e in (by_id[d] for d in role):
        for t in e.tags:
            if t.derive is None:
                continue
            # 계산 태그는 입력이 움직이면 따라 움직인다. 정답지에 반응 시작을 남기려고 입력의 효과를 크기 0 으로 적는다
            inherited = [Applied(a.stage, a.start, a.effect, 0.0, a.damage) for i in t.derive.inputs for a in found.get((e.equip_id, i), ())]
            if inherited:
                found.setdefault((e.equip_id, t.tag_id), []).extend(inherited)
    boost: dict[TagKey, TagBoost] = {}
    factors = _factor_refs(mode.boost, by_id[equip_id], control) if mode.boost and mode.boost.factors else ()
    if factors:
        b = mode.boost
        for t in by_id[equip_id].tags:
            if t.signal in b.signals:
                boost[(equip_id, t.tag_id)] = TagBoost(None, 0.0, b.max_gain, factors=factors, gain=b.gain, shapes=tuple(b.shapes))
    src = next((t for t in by_id[equip_id].tags if mode.boost and mode.boost.by and t.signal == mode.boost.by
                and (not mode.boost.equip_ids or equip_id in mode.boost.equip_ids)), None)
    if src is not None:
        b, by = mode.boost, (equip_id, src.tag_id)
        for t in by_id[equip_id].tags:
            if t.signal in b.signals:
                key = (equip_id, t.tag_id)
                power = (control[by][0] / src.control.mean, control[by][1] / src.control.mean, b.exponent) if b.exponent else None
                boost[key] = TagBoost(by, (b.per_unit or 0.0) * control[by][1], b.max_gain,
                                      _hold(found.get(key, ()), t.limits, *control[key]) if b.hold_below_alarm else None, power)
    out = {}
    signals = {(e.equip_id, t.tag_id): t.signal for e in plant.equipment for t in e.tags}
    for key, a in found.items():
        via, hop = role[key[0]]
        out[key] = TagPlan(tuple(a), quiet, via, hop, recovery_sec(signals[key]) * (hop + 1), boost.get(key))
    return out


def _factor_refs(b: Boost, e: EquipmentConfig, control: dict[TagKey, tuple[float, float]]) -> tuple[FactorRef, ...]:
    """설비의 요인 태그. 요인 하나라도 태그가 없거나 대상 설비가 아니면 빈 값 (교호작용 없음)."""
    if b.equip_ids and e.equip_id not in b.equip_ids:
        return ()
    first = {}
    for t in e.tags:
        first.setdefault(t.signal, (e.equip_id, t.tag_id))
    refs = []
    for f in b.factors:
        if f.signal not in first or (f.over and f.over not in first):
            return ()
        (mu, sg), over = control[first[f.signal]], None
        if f.over:
            k2 = first[f.over]
            over = (k2, control[k2][0] + f.offset, control[k2][1])
        refs.append(FactorRef(f, first[f.signal], (mu + f.offset, sg), over))
    return tuple(refs)


def factor_gain(boost: TagBoost, z_of) -> float:
    """요인 곱 교호작용 배율. z_of(태그) 는 그 태그의 step·ramp 사건 효과 z."""
    return min(boost.max_gain, 1 + boost.gain * math.prod(f.level(z_of) for f in boost.factors))


def _hold(applied, limits, mu: float, sigma: float) -> tuple[float, float, float, float] | None:
    """경고 구간 보장: 첫 경보 한계 목표 효과의 (시작, lag, rise_sec, |경보 z| − HOLD_MARGIN_Z). 그 태그에 경보 목표가 없으면 None."""
    for a in applied:
        name = a.effect.to_limit
        bound = getattr(limits, name) if name and name.startswith("alarm") and limits is not None else None
        if bound is not None:
            return a.start, a.effect.lag_sec, a.effect.rise_sec, abs(bound - mu) / sigma - HOLD_MARGIN_Z
    return None


def boost_gain(boost: TagBoost, z_by: float) -> float:
    """교호작용 배율. 선형: by 태그의 사건 효과 z 가 오른 만큼 키운다(내려가면 1).
    비율 거듭제곱: (운전점 + 사건 효과) / 설계 의 지수 거듭제곱 (낮으면 1 보다 작다)."""
    if boost.power is not None:
        r0, k, exp = boost.power
        return min(boost.max_gain, max(0.0, r0 + k * z_by) ** exp)
    return min(boost.max_gain, 1 + boost.per_z * max(0.0, z_by))


def boosted(boost: TagBoost, z: float, z_by: float, t: float, speed: int = 1) -> float:
    """사건 시작 기준 t 초의 효과 z 를 교호작용으로 키운 값. 경보 목표 효과가 시작되기 전에는 hold 상한(원래 효과가 이미
    넘었으면 원래 크기)을 넘지 않고, 시작 뒤 그 효과의 상승 시간(ramp 처럼 1/speed) 동안 상한을 키운 값까지 선형으로 푼다."""
    zb = z * boost_gain(boost, z_by)
    if boost.hold is None:
        return zb
    start, lag, rise, cap = boost.hold
    cap = max(cap, abs(z))
    if abs(zb) <= cap:
        return zb
    rise /= speed
    released = t - effect_start(start, lag, speed)
    frac = 0.0 if released < 0 else 1.0 if rise <= 0 or released >= rise else released / rise
    return math.copysign(cap + frac * (abs(zb) - cap), zb)


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


def tag_effect(tp: TagPlan, t: float, stop: float | None, speed: int = 1,
               repair: float | None = None, shapes: tuple[Shape, ...] = ()) -> tuple[float, bool]:
    """shapes 를 주면 그 모양의 효과만 더한다. 사건 시작 기준 t 초(실제 시간)의 (효과 z 합, nelson_quiet 여부). stop·repair 도 사건 시작 기준(None 이면 아직 안 함).
    효과 시작은 단계 시각/speed + lag (effect_start). 멈추기 전에 시작하지 못한 효과는 없고,
    멈춘 뒤에는 tp.recovery 동안 선형으로 0 이 된다. 손상 효과(a.damage)는 멈춘 시각의 크기로 남아 있다가
    수리 뒤 tp.recovery 동안 0 이 된다."""
    stopped = stop is not None and t >= stop
    fade = max(0.0, 1 - (t - stop) / tp.recovery) if stopped else 1.0
    healed = 1.0 if repair is None or t < repair else max(0.0, 1 - (t - repair) / tp.recovery)
    z = 0.0
    for a in tp.applied:
        if shapes and a.effect.shape not in shapes:
            continue
        s = effect_start(a.start, a.effect.lag_sec, speed)
        if s > t or (stop is not None and s >= stop):
            continue
        if stopped and a.damage:
            z += _shape(a, stop - s, speed) * healed
        else:
            z += _shape(a, t - s, speed) * fade
    quiet = not stopped and any(lo / speed <= t < hi / speed for lo, hi in tp.quiet)
    return z, quiet


def hydraulic_plan(mode: FaultMode) -> dict[HydraulicParameter, TagPlan]:
    """물리 파라미터에도 기존 ramp·손상·수리 시간 함수를 적용한다."""
    effects: dict[HydraulicParameter, list[Applied]] = {}
    for stage in mode.stages:
        for change in stage.hydraulic:
            effect = Effect(signal=change.parameter, shape="ramp", z=change.amount, rise_sec=change.rise_sec)
            effects.setdefault(change.parameter, []).append(
                Applied(stage.id, stage.at_sec, effect, change.amount, stage.damage)
            )
    return {key: TagPlan(tuple(changes), (), via="physics", recovery=recovery_sec(key)) for key, changes in effects.items()}


def event_tag_effect(tp: TagPlan, event: FaultEvent, epoch: float, shapes: tuple[Shape, ...] = ()) -> tuple[float, bool]:
    """절대 시각의 사건 효과. 태그와 물리 파라미터가 같은 수명 주기를 쓴다."""
    return tag_effect(tp, epoch - event.start, None if event.stop is None else event.stop - event.start,
                      event.speed, None if event.repair is None else event.repair - event.start, shapes)


def holds_damage(tags: dict[TagKey, TagPlan], ev: FaultEvent) -> bool:
    """멈춘 사건에 수리 전까지 남는 손상 효과가 있는가 (손상 단계의 step·ramp 가 멈추기 전에 시작했다)."""
    if ev.stop is None:
        return False
    return any(a.damage and effect_start(a.start, a.effect.lag_sec, ev.speed) < ev.stop - ev.start
               for tp in tags.values() for a in tp.applied)
