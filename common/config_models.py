"""설정 모델 (기획서 4장, RFCC 설계 §4.1).

구성안(plant_hds.yaml / plant_rfcc.yaml)과 실행 설정(runtime*.yaml)을 Pydantic으로 읽고,
시작 시점에 잘못된 설정을 걸러 낸다. 설비·태그 ID는 이 파일을 통해 읽기만 하며,
코드는 ID 형식을 해석하거나 특정 ID를 하드코딩하지 않는다(기획서 3.4).
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ConfigError(Exception):
    """설정 파일 내용이 서로 맞지 않을 때(없는 ID 참조 등). 시작을 멈추고 원인을 알려 준다."""


TagKey = tuple[str, str]        # (equip_id, tag_id). 태그명은 같은 설비 안에서만 고유하다 (다중 공장 설계 §4.1)


# ---------------------------------------------------------------------------
# 구성안 (plant_hds.yaml)
# ---------------------------------------------------------------------------

class StrictModel(BaseModel):
    """오타로 들어간 알 수 없는 키를 거부하는 공통 베이스."""

    model_config = ConfigDict(extra="forbid")


class Limits(StrictModel):
    """태그 한계값. 상한(…_hi)은 10분 최대값이 넘으면, 하한(…_lo)은 10분 최소값이 내려가면 알람. 자료에 있는 칸만 적는다."""

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


class NormalModel(StrictModel):
    """정상 운전 신호의 모양 (기획서 5.1)."""

    base: float               # 기준값
    noise_sd: float           # 작은 잡음의 표준편차
    daily_amp: float = 0.0    # 하루 주기 변동 진폭 (hds 모델만)
    drift_sd: float = 0.0     # 천천히 떠도는 성분(시간당 랜덤 워크 걸음)의 표준편차 (hds 모델만)


class ControlLimits(StrictModel):
    """Nelson 관리도의 고정 관리 기준: 중심 μ 와 표준편차 σ (팀의 baselines.json)."""

    mean: float
    sigma: float = Field(gt=0)


class TagConfig(StrictModel):
    tag_id: str
    signal: str                           # signals 목록의 signal 값. 신호 모델이 식을 고르는 키
    unit: str
    location: str | None = None           # 측정 위치 코드 (hds)
    description: str | None = None        # 태그 설명 (rfcc). 화면과 equipment_tag 에 남긴다
    limits: Limits | None = None          # 없으면 알람 판정 대상이 아니다 (rfcc)
    normal_ref: str | None = None         # 자료의 "정상 기준(참고)" 원문 (예: "1.4~2.8"). 코드는 해석하지 않는다
    limit_basis: str | None = None        # 자료의 "한계값 근거" 원문
    delay_samples: int = Field(default=1, ge=1)   # 알람 지연 판정: 연속된 10분 구간 수
    interval_sec: int | None = Field(default=None, ge=1)   # 태그별 측정 주기. 없으면 runtime.sample_interval_sec
    decimals: int = Field(default=3, ge=0)        # 값 반올림 자릿수
    control: ControlLimits | None = None  # Nelson 관리 기준 μ·σ. HDS 구성안이 남아 있는 동안만 선택
    seed_key: str                         # 시드용 고정 키. ID를 바꿔도 생성 값이 유지된다
    normal: NormalModel


class EquipmentConfig(StrictModel):
    equip_id: str
    name: str
    type: str                         # equipment_types 목록의 type 값
    grade: int | None = None
    unit_id: str
    aliases: list[str] = []
    driven_by: str | None = None      # 구동기 설비 ID. 구동기 이상이 피구동 설비 진동으로 번질 때, 축 속도를 공유할 때 쓴다
    seed_key: str | None = None       # 설비 단위 성분(rfcc 의 설비 부하)의 시드. signal_model rfcc 면 필수
    tags: list[TagConfig]


class UnitConfig(StrictModel):
    id: str
    name: str


class UnitGroupConfig(StrictModel):
    id: str
    name: str
    units: list[UnitConfig]


SignalModelName = Literal["hds", "rfcc"]


class PlantMeta(StrictModel):
    plant_id: str
    name: str
    config_version: int
    signal_model: SignalModelName = "hds"     # 값을 만드는 신호 모델 (simulator.generator.make_signal_model)
    unit_groups: list[UnitGroupConfig]


class EquipmentType(StrictModel):
    """설비 유형 매핑 (DB 유형 매핑 테이블의 원본)."""

    type: str
    name: str
    prefix: str
    kind: str | None = None           # 신호 모델이 보는 기계 종류 (pump / blower / expander / compressor / turbine)


class SignalType(StrictModel):
    """표준 태그 (신호 접두어·단위)."""

    signal: str
    prefix: str
    unit: str
    name: str


class Correlation(StrictModel):
    """태그끼리 함께 움직이는 규칙 (기획서 5.2). target += gain × source 편차(lag_minutes 전). source/target 은 TagRef."""

    source: str                       # "EQUIP/TAG" 또는 공장 안에서 고유한 "TAG"
    target: str
    gain: float
    lag_minutes: int = Field(ge=0)


class PlantConfig(StrictModel):
    plant: PlantMeta
    equipment_types: list[EquipmentType]
    signals: list[SignalType]
    equipment: list[EquipmentConfig]
    correlations: list[Correlation] = []

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
        if self.plant.signal_model == "rfcc":
            for e in self.equipment:
                if e.seed_key is None:
                    raise ConfigError(f"equipment {e.equip_id}: seed_key is required for signal_model rfcc")
            _require_unique("equipment seed_key", [e.seed_key for e in self.equipment])

        equip_set = set(equip_ids)
        for e in self.equipment:
            if e.unit_id not in unit_ids:
                raise ConfigError(f"equipment {e.equip_id}: unknown unit_id {e.unit_id}")
            if e.type not in type_ids:
                raise ConfigError(f"equipment {e.equip_id}: unknown type {e.type}")
            if e.driven_by is not None and e.driven_by not in equip_set:
                raise ConfigError(f"equipment {e.equip_id}: driven_by references unknown equipment {e.driven_by}")
            for t in e.tags:
                if t.signal not in signal_ids:
                    raise ConfigError(f"tag {t.tag_id}: unknown signal {t.signal}")
        for c in self.correlations:
            try:
                source, target = self.resolve(c.source), self.resolve(c.target)
            except ConfigError as e:
                raise ConfigError(f"correlation {c.source}->{c.target}: {e}") from None
            if self.tag_unit(source) != self.tag_unit(target):
                raise ConfigError(f"correlation {c.source}->{c.target}: tags must belong to the same unit")
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

    def tag_unit(self, key: TagKey) -> str:
        return self.equipment_by_id()[key[0]].unit_id

    def tag_interval_sec(self, key: TagKey, default: int) -> int:
        """태그의 측정 주기. 구성안에 없으면 실행 설정의 sample_interval_sec(default)."""
        return self.tag_by_key()[key].interval_sec or default

    def resolve(self, ref: str, equip_hint: str | None = None) -> TagKey:
        """TagRef → 키. "EQUIP/TAG" 는 그대로, "TAG" 는 equip_hint 설비에 있으면 그것, 아니면 공장 안에서 유일할 때만."""
        keys = self.tag_by_key()
        if "/" in ref:
            equip_id, tag_id = ref.split("/", 1)
            if (equip_id, tag_id) not in keys:
                raise ConfigError(f"unknown tag {ref}")
            return equip_id, tag_id
        if equip_hint is not None and (equip_hint, ref) in keys:
            return equip_hint, ref
        candidates = sorted(k for k in keys if k[1] == ref)
        if not candidates:
            raise ConfigError(f"unknown tag {ref}")
        if len(candidates) > 1:
            raise ConfigError(f"tag {ref} is ambiguous: " + ", ".join(f"{e}/{t}" for e, t in candidates) + " (write EQUIP/TAG)")
        return candidates[0]

    def equipment_kind(self, equip_id: str) -> str | None:
        """설비 유형(equipment_types)의 kind."""
        kinds = {t.type: t.kind for t in self.equipment_types}
        return kinds[self.equipment_by_id()[equip_id].type]

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


def _require_unique(label: str, values: list[str]) -> None:
    dupes = [v for v, n in Counter(values).items() if n > 1]
    if dupes:
        raise ConfigError(f"duplicate {label}: {', '.join(dupes)}")


# ---------------------------------------------------------------------------
# 실행 설정 (runtime.yaml) — 기획서 4.2 표의 항목과 기본값
# ---------------------------------------------------------------------------

TimeMode = Literal["backfill", "live", "accelerated"]


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
    persistent_fault_interval_min: float = Field(default=15, ge=0)   # 지속 고장을 여는 간격(분). 0 이면 끈다

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
