"""설정 모델 (기획서 4장, RFCC 설계 §4.1).

구성안(plant_<key>.yaml)과 실행 설정(runtime_<key>.yaml)을 Pydantic으로 읽고,
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
    control: ControlLimits                # Nelson 관리 기준 μ·σ. 생성과 판정이 모두 이 값을 쓴다
    seed_key: str                         # 시드용 고정 키. ID를 바꿔도 생성 값이 유지된다


class EquipmentConfig(StrictModel):
    equip_id: str
    name: str
    type: str                         # equipment_types 목록의 type 값
    grade: int | None = None
    unit_id: str
    aliases: list[str] = []
    driven_by: str | None = None      # 구동기 설비 ID. 구동기 이상이 피구동 설비 진동으로 번질 때, 축 속도를 공유할 때 쓴다
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


class PlantConfig(StrictModel):
    plant: PlantMeta
    equipment_types: list[EquipmentType]
    signals: list[SignalType]
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
            for t in e.tags:
                if t.signal not in signal_ids:
                    raise ConfigError(f"tag {t.tag_id}: unknown signal {t.signal}")
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
# 실행 설정 (runtime_<key>.yaml) — 기획서 4.2 표의 항목과 기본값
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
