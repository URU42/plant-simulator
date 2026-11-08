"""설정 모델 (기획서 4장).

plant_hds.yaml(구성안)과 runtime.yaml(실행 설정)을 Pydantic으로 읽고,
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


# ---------------------------------------------------------------------------
# 구성안 (plant_hds.yaml)
# ---------------------------------------------------------------------------

class StrictModel(BaseModel):
    """오타로 들어간 알 수 없는 키를 거부하는 공통 베이스."""

    model_config = ConfigDict(extra="forbid")


class Limits(StrictModel):
    """태그 한계값. 시뮬레이터는 정상값을 warn_hi 아래에 두고, 수집 서버는 같은 값으로 알람을 판정한다."""

    warn_hi: float
    alarm_hi: float
    trip_hi: float

    @model_validator(mode="after")
    def _ordered(self) -> "Limits":
        if not (self.warn_hi < self.alarm_hi < self.trip_hi):
            raise ValueError("limits must satisfy warn_hi < alarm_hi < trip_hi")
        return self


class NormalModel(StrictModel):
    """정상 운전 신호의 모양 (기획서 5.1)."""

    base: float          # 기준값
    noise_sd: float      # 작은 잡음의 표준편차
    daily_amp: float     # 하루 주기 변동 진폭
    drift_sd: float      # 천천히 떠도는 성분(시간당 랜덤 워크 걸음)의 표준편차


class TagConfig(StrictModel):
    tag_id: str
    signal: str                      # signals 목록의 signal 값 (vibration / temperature / pressure)
    unit: str
    location: str
    limits: Limits
    delay_samples: int = Field(ge=1)  # 알람 지연 판정: 연속된 10분 구간 수
    seed_key: str                     # 시드용 고정 키. ID를 바꿔도 생성 값이 유지된다
    normal: NormalModel


class EquipmentConfig(StrictModel):
    equip_id: str
    name: str
    type: str                         # equipment_types 목록의 type 값
    grade: int
    unit_id: str
    aliases: list[str] = []
    driven_by: str | None = None      # 구동기 설비 ID. 구동기 이상이 피구동 설비 진동으로 번질 때 쓴다
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


class SignalType(StrictModel):
    """표준 태그 (신호 접두어·단위)."""

    signal: str
    prefix: str
    unit: str
    name: str


class Correlation(StrictModel):
    """태그끼리 함께 움직이는 규칙 (기획서 5.2). target += gain × source 편차(lag_minutes 전)."""

    source: str
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
        tag_ids = [t.tag_id for e in self.equipment for t in e.tags]
        seed_keys = [t.seed_key for e in self.equipment for t in e.tags]

        _require_unique("equip_id", equip_ids)
        _require_unique("tag_id", tag_ids)
        _require_unique("seed_key", seed_keys)

        equip_set, tag_set = set(equip_ids), set(tag_ids)
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
            for ref in (c.source, c.target):
                if ref not in tag_set:
                    raise ConfigError(f"correlation {c.source}->{c.target}: unknown tag {ref}")
            if self.tag_unit(c.source) != self.tag_unit(c.target):
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

    def tag_owner(self) -> dict[str, EquipmentConfig]:
        """tag_id → 그 태그를 가진 설비."""
        return {t.tag_id: e for e in self.equipment for t in e.tags}

    def tag_by_id(self) -> dict[str, TagConfig]:
        return {t.tag_id: t for t in self.all_tags()}

    def tag_unit(self, tag_id: str) -> str:
        return self.tag_owner()[tag_id].unit_id

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

    @model_validator(mode="after")
    def _intervals(self) -> "RuntimeConfig":
        if self.send_interval_sec % self.sample_interval_sec:
            raise ValueError("send_interval_sec must be a multiple of sample_interval_sec")
        if 86400 % self.sample_interval_sec:
            raise ValueError("sample_interval_sec must divide a day (86400 s)")
        return self


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
