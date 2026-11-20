"""설정 모델 (기획서 4장, RFCC 설계 §4.1).

구성안(plant_<key>.yaml)과 실행 설정(runtime_<key>.yaml)을 Pydantic으로 읽고,
시작 시점에 잘못된 설정을 걸러 낸다. 설비·태그 ID는 이 파일을 통해 읽기만 하며,
코드는 ID 형식을 해석하거나 특정 ID를 하드코딩하지 않는다(기획서 3.4).
"""
from __future__ import annotations

import math
from collections import Counter
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ConfigError(Exception):
    """설정 파일 내용이 서로 맞지 않을 때(없는 ID 참조 등). 시작을 멈추고 원인을 알려 준다."""


TagKey = tuple[str, str]        # (equip_id, tag_id). 태그명은 같은 설비 안에서만 고유하다 (다중 공장 설계 §4.1)


# ---------------------------------------------------------------------------
# 구성안 (plant_<key>.yaml)
# ---------------------------------------------------------------------------

class StrictModel(BaseModel):
    """오타로 들어간 알 수 없는 키를 거부하는 공통 베이스."""

    model_config = ConfigDict(extra="forbid")


class Limits(StrictModel):
    """태그 한계값 (자료의 참고값, 판정에는 쓰지 않는다). 자료에 있는 칸만 적는다."""

    warn_hi: float | None = None
    alarm_hi: float | None = None
    trip_hi: float | None = None
    warn_lo: float | None = None
    alarm_lo: float | None = None
    trip_lo: float | None = None

    @model_validator(mode="after")
    def _ordered(self) -> "Limits":
        his = [v for _, v in self.hi_levels()]
        los = [v for _, v in self.lo_levels()]
        if not his and not los:
            raise ValueError("limits must have at least one bound")
        if his != sorted(his) or len(set(his)) != len(his):
            raise ValueError("limits must satisfy warn_hi < alarm_hi < trip_hi")
        if los != sorted(los, reverse=True) or len(set(los)) != len(los):
            raise ValueError("limits must satisfy trip_lo < alarm_lo < warn_lo")
        if his and los and max(los) >= min(his):
            raise ValueError("every lower limit must be below every upper limit")
        return self

    def hi_levels(self) -> list[tuple[str, float]]:
        """[(한계 구분, 값)] — 있는 상한만, 낮은 것부터. 구분은 Soft / Alarm / Trip."""
        return [(name, v) for name, v in (("Soft", self.warn_hi), ("Alarm", self.alarm_hi), ("Trip", self.trip_hi)) if v is not None]

    def lo_levels(self) -> list[tuple[str, float]]:
        """[(한계 구분, 값)] — 있는 하한만, 높은 것부터."""
        return [(name, v) for name, v in (("Soft", self.warn_lo), ("Alarm", self.alarm_lo), ("Trip", self.trip_lo)) if v is not None]


class ControlLimits(StrictModel):
    """Nelson 관리도의 고정 관리 기준: 중심 μ 와 표준편차 σ (팀의 baselines.json)."""

    mean: float
    sigma: float = Field(gt=0)


# IAPWS-IF97 4영역 역방향 식 T_sat(p) 의 계수 n1~n10
_IF97 = (0.11670521452767e4, -0.72421316703206e6, -0.17073846940092e2, 0.12020824702470e5, -0.32325550322333e7,
         0.14915108613530e2, -0.48232657361591e4, 0.40511340542057e6, -0.23855557567849, 0.65017534844798e3)


def saturation_temp(p_mpa: float) -> float:
    """물의 포화 온도(℃), 절대압 MPa (IAPWS-IF97 식 31, 611 Pa ~ 22.064 MPa)."""
    n1, n2, n3, n4, n5, n6, n7, n8, n9, n10 = _IF97
    b = p_mpa ** 0.25
    e, f, g = b * b + n3 * b + n6, n1 * b * b + n4 * b + n7, n2 * b * b + n5 * b + n8
    d = 2 * g / (-f - math.sqrt(f * f - 4 * e * g))
    return (n10 + d - math.sqrt((n10 + d) ** 2 - 4 * (n9 + n10 * d))) / 2 - 273.15


class Term(StrictModel):
    """power 식의 인자 하나: ((tag − minus) + offset) 를 설계점 값으로 나눈 비의 exp 제곱."""

    tag: str
    minus: str | None = None               # 차를 쓸 때 빼는 태그 (예: 양정 = 토출 − 흡입)
    offset: float = 0.0                    # 절대값으로 옮기는 값 (℃ → K 273.15, 게이지압 → 절대압 대기압)
    exp: float


class Derive(StrictModel):
    """계산 태그: 같은 설비 태그(inputs)의 그 시각 값으로 만든다. 값 = 계산식 + 자체 잡음(noise_sigma) + 고장 효과.
    - steam_saturation_temp: 입력 1개(증기 압력, bar(g)) → 포화 온도 ℃ + offset(과열도)
    - geometric_mean: 입력 2개(게이지압) → √(절대압 곱) − atm (2단·단별 압력비 같음 가정의 단간 압력)
    - power: (값 + out_offset) = (설계 μ + out_offset) × Π(인자 비 ^ exp). 설계점에서 설계 μ 가 되고, 작은 변화는 탄성(exp)대로 따른다.
      inputs 는 terms 에서 채운다."""

    formula: Literal["steam_saturation_temp", "geometric_mean", "power"]
    inputs: list[str] = []                 # 같은 설비의 tag_id
    atm: float = Field(default=0.0, ge=0)  # 게이지압 → 절대압 (입력 단위의 대기압)
    offset: float = 0.0
    terms: list[Term] = []
    out_offset: float = 0.0
    noise_sigma: float = Field(ge=0)       # 입력에 없는 자체 변동 σ (태그 단위)

    @model_validator(mode="after")
    def _arity(self) -> "Derive":
        if self.formula == "power":
            if not self.terms or self.inputs:
                raise ValueError("power needs terms (and no inputs)")
            self.inputs = list(dict.fromkeys(i for t in self.terms for i in (t.tag, t.minus) if i))
            return self
        need = {"steam_saturation_temp": 1, "geometric_mean": 2}[self.formula]
        if len(self.inputs) != need or self.terms:
            raise ValueError(f"{self.formula} needs {need} input(s) and no terms")
        return self

    def value(self, xs: list[float], ref: list[float], ref_out: float) -> float:
        """입력 값 xs(inputs 순서)의 계산값. ref·ref_out 는 설계점(구성안 control.mean)의 입력·출력 값 (power 만 쓴다)."""
        if self.formula == "steam_saturation_temp":
            return saturation_temp((xs[0] + self.atm) / 10) + self.offset
        if self.formula == "geometric_mean":
            return math.sqrt((xs[0] + self.atm) * (xs[1] + self.atm)) - self.atm + self.offset
        at, base = dict(zip(self.inputs, xs)), dict(zip(self.inputs, ref))
        y = ref_out + self.out_offset
        for t in self.terms:
            cur = at[t.tag] - (at[t.minus] if t.minus else 0.0) + t.offset
            des = base[t.tag] - (base[t.minus] if t.minus else 0.0) + t.offset
            y *= (cur / des) ** t.exp
        return y - self.out_offset


class TagConfig(StrictModel):
    tag_id: str
    signal: str                           # signals 목록의 signal 값 (신호 종류)
    unit: str
    description: str | None = None        # 태그 설명. 화면과 equipment_tag 에 남긴다
    limits: Limits | None = None          # 자료의 참고 한계값. 없으면 비운다
    normal_ref: str | None = None         # 자료의 "정상 기준(참고)" 원문 (예: "1.4~2.8"). 코드는 해석하지 않는다
    limit_basis: str | None = None        # 자료의 "한계값 근거" 원문
    interval_sec: int | None = Field(default=None, ge=1)   # 태그별 측정 주기. 없으면 runtime.sample_interval_sec
    decimals: int = Field(default=3, ge=0)        # 값 반올림 자릿수
    control: ControlLimits                # 설계 운전점의 관리 기준 μ·σ. 운전점(operating)이 다르면 μ 는 control_by_key 가 고친다
    seed_key: str                         # 시드용 고정 키. ID를 바꿔도 생성 값이 유지된다
    derive: Derive | None = None          # 계산 태그. control.mean 은 설계점 입력 μ 로 계산한 값과 같아야 한다


class Operating(StrictModel):
    """설비의 시작 운전점. 설계점(speed 태그의 control.mean)과의 비 r 로 이 설비와 피구동 설비들의 태그 μ 를 고친다."""

    speed_rpm: float = Field(gt=0)


class EquipmentConfig(StrictModel):
    equip_id: str
    name: str
    type: str                         # equipment_types 목록의 type 값
    grade: int | None = None
    unit_id: str
    aliases: list[str] = []
    driven_by: str | None = None      # 구동기 설비 ID. 축 속도를 공유하고, 고장 모드가 피구동 설비로 번질 때 쓴다
    operating: Operating | None = None   # 시작 운전점. 구동기(driven_by 없음)이고 speed 태그가 있어야 한다
    tags: list[TagConfig]


class UnitConfig(StrictModel):
    id: str
    name: str


class UnitGroupConfig(StrictModel):
    id: str
    name: str
    units: list[UnitConfig]


class PlantMeta(StrictModel):
    plant_id: str
    name: str
    config_version: int
    unit_groups: list[UnitGroupConfig]


class EquipmentType(StrictModel):
    """설비 유형 매핑 (DB 유형 매핑 테이블의 원본)."""

    type: str
    name: str
    prefix: str
    kind: str | None = None           # 기계 종류 (pump / blower / expander / compressor / turbine). 참고용


class SignalType(StrictModel):
    """표준 태그 (신호 접두어·단위)."""

    signal: str
    prefix: str
    unit: str
    name: str


class Correlation(StrictModel):
    """정상 운전의 공통 요인: 공정 그룹(unit group)마다 평균 0 인 빠른 성분 하나를 그 그룹 태그들이 나눠 갖는다.
    signs 에 있는 신호의 태그만 참여하고, 태그 분산 중 share 만큼이 공통 요인이다 (부호는 신호의 방향)."""

    share: float = Field(gt=0, lt=1)
    signs: dict[str, Literal[-1, 1]]


# 운전점 비 r(설계 rpm 대비)에 대한 태그 μ 의 지수 (상사 법칙 근사, synthetic). 없는 신호는 바뀌지 않는다.
# discharge_pressure 는 같은 설비 첫 suction_pressure 와의 차(양정)에 r² 를 곱한다.
AFFINITY = {"speed": 1, "flow": 1, "current": 3, "vibration": 2, "shaft_vibration": 2, "differential_pressure": 2}
HEAD_EXPONENT = 2


class PlantConfig(StrictModel):
    plant: PlantMeta
    equipment_types: list[EquipmentType]
    signals: list[SignalType]
    correlation: Correlation | None = None
    equipment: list[EquipmentConfig]

    # --- 검증 -----------------------------------------------------------
    @model_validator(mode="after")
    def _cross_check(self) -> "PlantConfig":
        """파일 안에서 서로 가리키는 ID가 모두 존재하는지 확인한다. 빠진 ID는 이름으로 알려 준다."""
        unit_ids = set(self.unit_ids())
        type_ids = {t.type for t in self.equipment_types}
        signal_ids = {s.signal for s in self.signals}
        equip_ids = [e.equip_id for e in self.equipment]
        seed_keys = [t.seed_key for e in self.equipment for t in e.tags]

        _require_unique("equip_id", equip_ids)
        _require_unique("(equip_id, tag_id)", [f"{e.equip_id}/{t.tag_id}" for e in self.equipment for t in e.tags])
        _require_unique("seed_key", seed_keys)

        equip_set = set(equip_ids)
        for e in self.equipment:
            if e.unit_id not in unit_ids:
                raise ConfigError(f"equipment {e.equip_id}: unknown unit_id {e.unit_id}")
            if e.type not in type_ids:
                raise ConfigError(f"equipment {e.equip_id}: unknown type {e.type}")
            if e.driven_by is not None and e.driven_by not in equip_set:
                raise ConfigError(f"equipment {e.equip_id}: driven_by references unknown equipment {e.driven_by}")
            by_tag = {t.tag_id: t for t in e.tags}
            for t in e.tags:
                if t.signal not in signal_ids:
                    raise ConfigError(f"tag {t.tag_id}: unknown signal {t.signal}")
                if t.derive is not None:
                    _check_derive(e.equip_id, t, by_tag)
            if e.operating is not None:
                if e.driven_by is not None:
                    raise ConfigError(f"equipment {e.equip_id}: operating is set on the driver ({e.driven_by}), not on driven equipment")
                if not any(t.signal == "speed" for t in e.tags):
                    raise ConfigError(f"equipment {e.equip_id}: operating.speed_rpm needs a speed tag")
        if self.correlation is not None:
            unknown = sorted(set(self.correlation.signs) - signal_ids)
            if unknown:
                raise ConfigError(f"correlation.signs: unknown signal(s) {', '.join(unknown)}")
        return self

    # --- 조회 도우미 ---------------------------------------------------------
    def unit_ids(self) -> list[str]:
        return [u.id for g in self.plant.unit_groups for u in g.units]

    def all_tags(self) -> list[TagConfig]:
        return [t for e in self.equipment for t in e.tags]

    def type_counts(self) -> dict[str, int]:
        return dict(Counter(e.type for e in self.equipment))

    def equipment_by_id(self) -> dict[str, EquipmentConfig]:
        return {e.equip_id: e for e in self.equipment}

    def tag_keys(self) -> list[TagKey]:
        """(equip_id, tag_id) 목록. 구성안 순서."""
        return [(e.equip_id, t.tag_id) for e in self.equipment for t in e.tags]

    def tag_by_key(self) -> dict[TagKey, TagConfig]:
        return {(e.equip_id, t.tag_id): t for e in self.equipment for t in e.tags}

    def tag_interval_sec(self, key: TagKey, default: int) -> int:
        """태그의 측정 주기. 구성안에 없으면 실행 설정의 sample_interval_sec(default)."""
        return self.tag_by_key()[key].interval_sec or default

    def unit_group_of(self) -> dict[str, str]:
        """unit_id → unit_group id."""
        return {u.id: g.id for g in self.plant.unit_groups for u in g.units}

    def speed_ratio(self) -> dict[str, float]:
        """설비별 운전점 비 r = 운전 rpm / 설계 rpm. 피구동 설비는 맨 앞 구동기의 r 을 따른다. 운전점이 없으면 1."""
        by_id = self.equipment_by_id()
        own = {e.equip_id: e.operating.speed_rpm / next(t.control.mean for t in e.tags if t.signal == "speed")
               for e in self.equipment if e.operating is not None}

        def root(e: EquipmentConfig) -> str:
            seen = set()
            while e.driven_by is not None and e.equip_id not in seen:     # 순환 구성은 거기서 멈춘다
                seen.add(e.equip_id)
                e = by_id[e.driven_by]
            return e.equip_id

        return {e.equip_id: own.get(root(e), 1.0) for e in self.equipment}

    def control_by_key(self) -> dict[TagKey, tuple[float, float]]:
        """운전점을 반영한 태그별 (μ, σ). 생성기·설비 마스터·탐지 쪽 기준이 모두 이 값을 쓴다. σ 는 바꾸지 않는다."""
        ratio = self.speed_ratio()
        out: dict[TagKey, tuple[float, float]] = {}
        for e in self.equipment:
            r = ratio[e.equip_id]
            suction = next((t.control.mean for t in e.tags if t.signal == "suction_pressure"), None)
            for t in e.tags:
                mu = t.control.mean
                if r != 1.0:
                    if t.signal in AFFINITY:
                        mu *= r ** AFFINITY[t.signal]
                    elif t.signal == "discharge_pressure" and suction is not None:
                        mu = suction + (mu - suction) * r ** HEAD_EXPONENT
                out[(e.equip_id, t.tag_id)] = (mu, t.control.sigma)
            design = {t.tag_id: t.control.mean for t in e.tags}
            for t in e.tags:
                if t.derive is not None:        # 계산 태그는 운전점이 옮긴 입력 μ 로 다시 계산한다
                    ins = t.derive.inputs
                    mu = t.derive.value([out[(e.equip_id, i)][0] for i in ins], [design[i] for i in ins], t.control.mean)
                    out[(e.equip_id, t.tag_id)] = (mu, t.control.sigma)
        return out

    def equipment_for_units(self, units: list[str]) -> list[EquipmentConfig]:
        """runtime.units 에 맞는 설비 목록. `all`이면 전체, 아니면 적힌 Unit만."""
        if units == ["all"]:
            return list(self.equipment)
        known = set(self.unit_ids())
        missing = [u for u in units if u not in known]
        if missing:
            raise ConfigError(f"runtime.units references unknown unit(s): {', '.join(missing)}")
        wanted = set(units)
        return [e for e in self.equipment if e.unit_id in wanted]


def _check_derive(equip_id: str, t: TagConfig, by_tag: dict[str, TagConfig]) -> None:
    """입력은 같은 설비의 계산 태그가 아닌 태그이고, 계산 태그의 측정 시각마다 값이 있어야(주기가 나눠떨어져야) 한다."""
    ins: list[float] = []
    for i in t.derive.inputs:
        src = by_tag.get(i)
        if src is None or src.derive is not None:
            raise ConfigError(f"tag {equip_id}/{t.tag_id}: derive input {i} must be a non-derived tag of the same equipment")
        if (t.interval_sec or 1) % (src.interval_sec or 1):
            raise ConfigError(f"tag {equip_id}/{t.tag_id}: interval must be a multiple of input {i}'s interval")
        ins.append(src.control.mean)
    if abs(t.derive.value(ins, ins, t.control.mean) - t.control.mean) > t.control.sigma:
        raise ConfigError(f"tag {equip_id}/{t.tag_id}: control.mean differs from the derive formula at the inputs' mean")


def _require_unique(label: str, values: list[str]) -> None:
    dupes = [v for v, n in Counter(values).items() if n > 1]
    if dupes:
        raise ConfigError(f"duplicate {label}: {', '.join(dupes)}")


# ---------------------------------------------------------------------------
# 실행 설정 (runtime_<key>.yaml) — 기획서 4.2 표의 항목과 기본값
# ---------------------------------------------------------------------------

TimeMode = Literal["backfill", "live", "accelerated"]


class SpikeConfig(StrictModel):
    per_tag_per_day: float = Field(default=1.0, ge=0)
    z_min: float = Field(default=4.0, gt=0)
    z_max: float = Field(default=8.0, gt=0)


class StuckConfig(StrictModel):
    per_equipment_per_week: float = Field(default=1.0, ge=0)
    min_sec: int = Field(default=300, ge=1)
    max_sec: int = Field(default=1800, ge=1)


class DropoutConfig(StrictModel):
    per_plant_per_day: float = Field(default=3.0, ge=0)     # 한 번에 설비 하나의 모든 태그
    min_sec: int = Field(default=10, ge=1)
    max_sec: int = Field(default=300, ge=1)


class InstrumentFaults(StrictModel):
    """계측 이상 층 (simulator.instrument). 빈도·크기는 synthetic 가정. 기본은 꺼짐."""

    enabled: bool = False
    spike: SpikeConfig = SpikeConfig()
    stuck: StuckConfig = StuckConfig()
    dropout: DropoutConfig = DropoutConfig()

    @model_validator(mode="after")
    def _ranges(self) -> "InstrumentFaults":
        if self.spike.z_min > self.spike.z_max or self.stuck.min_sec > self.stuck.max_sec \
                or self.dropout.min_sec > self.dropout.max_sec or self.stuck.max_sec > 86400 or self.dropout.max_sec > 86400:
            raise ValueError("instrument_faults: min must not exceed max, durations at most a day")
        return self


class RuntimeConfig(StrictModel):
    units: list[str] = ["all"]
    source_id: str = "SIM-ALL"
    sample_interval_sec: int = Field(default=10, ge=1)
    send_interval_sec: int = Field(default=60, ge=1)
    time_mode: TimeMode = "live"
    speed: float = Field(default=60, gt=0)
    backfill_days: int = Field(default=90, ge=0)
    seed: int = 20261108
    collector_url: str = "http://localhost:8000"
    raw_retention_days: int = Field(default=7, ge=1)
    instrument_faults: InstrumentFaults = InstrumentFaults()
    # 정상 잡음을 Nelson 규칙 1~3 위반 없이 고른다(억제 표집, D3). false 면 거르지 않는다 — 학습·오탐률 평가용 (2026-11-20)
    quiet_normal: bool = True

    @model_validator(mode="after")
    def _intervals(self) -> "RuntimeConfig":
        if self.send_interval_sec % self.sample_interval_sec:
            raise ValueError("send_interval_sec must be a multiple of sample_interval_sec")
        if 86400 % self.sample_interval_sec:
            raise ValueError("sample_interval_sec must divide a day (86400 s)")
        return self

    def validate_tag_intervals(self, plant: PlantConfig) -> None:
        """태그별 주기는 sample_interval_sec 의 배수이고 하루를 나눠야 한다."""
        for t in plant.all_tags():
            iv = t.interval_sec
            if iv is not None and (iv % self.sample_interval_sec or 86400 % iv):
                raise ConfigError(f"tag {t.tag_id}: interval_sec {iv} must be a multiple of "
                                  f"sample_interval_sec {self.sample_interval_sec} and divide a day")


# ---------------------------------------------------------------------------
# 로더
# ---------------------------------------------------------------------------

def load_yaml(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_plant(path: str | Path) -> PlantConfig:
    return PlantConfig.model_validate(load_yaml(path))


def load_runtime(path: str | Path) -> RuntimeConfig:
    return RuntimeConfig.model_validate(load_yaml(path))
