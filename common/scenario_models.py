"""시나리오 파일 모델 (기획서 5.4).

scenario_dev.yaml(개발용)과 scenario_eval.yaml(평가용, 비공개)이 같은 형식을 쓴다.
평가용 전용 항목(trend, question_aliases)은 선택 필드로 두어 같은 로더로 읽는다.
"""
from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import numpy as np
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from common.config_models import ConfigError, PlantConfig, TagKey, load_yaml

_DURATION = re.compile(r"^(\d+)([smhd])$")
_UNIT_SEC = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> timedelta:
    """'90s' / '30m' / '12h' / '5d' 형식."""
    m = _DURATION.match(str(text).strip())
    if not m:
        raise ValueError(f"invalid duration {text!r} (use e.g. 30m, 12h, 5d)")
    return timedelta(seconds=int(m.group(1)) * _UNIT_SEC[m.group(2)])


class RelatedTag(BaseModel):
    """함께 움직일 태그와 배율. tag_id 는 TagRef("EQUIP/TAG" 또는 태그명). equip_id 는 validate_against 가 채운다."""

    model_config = ConfigDict(extra="forbid")
    tag_id: str
    ratio: float
    equip_id: str | None = None

    @property
    def key(self) -> TagKey:
        return self.equip_id, self.tag_id


class ScenarioEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    pattern: str                       # simulator.patterns.PATTERNS 의 이름
    start: AwareDatetime
    duration: timedelta
    tag_id: str | None = None          # 태그명(TagRef). 없으면 설비의 모든 태그에 적용
    equip_id: str | None = None        # 설비. 없으면 validate_against 가 tag_id 로 찾아 채운다(공장 안에서 유일할 때)
    magnitude: float = 0.0             # 값 패턴의 크기(태그 단위). freeze/quality/gap 은 쓰지 않는다
    quality: str = "Bad"               # quality 패턴일 때의 품질 (Bad / ScanOff)
    related: list[RelatedTag] = []
    spread_to_driven: float | None = None   # driven_by 로 이어진 피구동 설비 진동 태그에 번지는 비율
    # 연결 정보
    failure_id: str | None = None
    work_order_id: str | None = None
    doc_id: str | None = None
    recovered_at: AwareDatetime | None = None   # 정비 후 정상 복귀 시각. 없으면 start + duration 에 끝난다
    repeat_every: timedelta | None = None       # 있으면 start + k·repeat_every 마다 같은 이벤트가 다시 일어난다
    description: str | None = None
    # 평가용 파일에만
    trend: str | None = None                    # 정답 추이 방향: 상승 / 하강 / 안정
    question_aliases: list[str] = []            # 질문에 쓸 별칭

    @field_validator("duration", "repeat_every", mode="before")
    @classmethod
    def _duration(cls, v):
        return parse_duration(v) if isinstance(v, str) else v

    @model_validator(mode="after")
    def _target(self) -> "ScenarioEvent":
        if self.tag_id is None and self.equip_id is None:
            raise ValueError(f"event {self.event_id}: give equip_id or tag_id")
        if self.recovered_at is not None and self.recovered_at < self.start + self.duration:
            raise ValueError(f"event {self.event_id}: recovered_at must be after start + duration")
        if self.repeat_every is not None:
            if self.recovered_at is not None:
                raise ValueError(f"event {self.event_id}: repeat_every cannot be combined with recovered_at")
            if self.duration > self.repeat_every:
                raise ValueError(f"event {self.event_id}: duration must not exceed repeat_every")
        return self

    def occurrence_start(self, epochs: np.ndarray) -> np.ndarray:
        """각 epoch 가 속한(또는 직전) 회차의 시작 epoch. 반복이 없으면 start."""
        start = self.start.timestamp()
        if self.repeat_every is None:
            return np.full(len(epochs), start)
        period = self.repeat_every.total_seconds()
        k = np.maximum(np.floor((epochs - start) / period), 0)
        return start + k * period

    @property
    def end(self):
        """패턴이 적용되는 구간의 끝. 정비 복귀 시각이 있으면 그때까지 마지막 값을 유지한다."""
        return self.recovered_at or (self.start + self.duration)

    def target_keys(self, plant: PlantConfig) -> list[TagKey]:
        """이벤트가 직접 적용되는 태그 키. tag_id 가 없으면 설비의 모든 태그. validate_against 뒤에 부른다."""
        if self.tag_id is not None:
            return [(self.equip_id, self.tag_id)]
        return [(self.equip_id, t.tag_id) for t in plant.equipment_by_id()[self.equip_id].tags]


class MaintenanceEvent(BaseModel):
    """정비 이벤트 (기획서 5.6). 구간 동안 설비는 정지 상태, 끝난 시각을 정비 이력에 남긴다."""

    model_config = ConfigDict(extra="forbid")
    work_order_id: str
    equip_id: str
    start: AwareDatetime
    end: AwareDatetime
    description: str | None = None


class RandomAnomalies(BaseModel):
    """우연한 잡음 알람 (기획서 5.5)."""

    model_config = ConfigDict(extra="forbid")
    rate_per_tag_day: float = Field(default=0.0, ge=0)   # 태그당 하루 평균 발생 횟수


class RandomQuality(BaseModel):
    """우연한 품질 저하 (다중 공장 설계 §4.3). HOU 생성기 규칙의 일반화."""

    model_config = ConfigDict(extra="forbid")
    uncertain_rate: float = Field(default=0.0, ge=0)             # 1초 샘플당 Uncertain 확률 (10초 태그는 ×10)
    bad_segments_per_tag_week: float = Field(default=0.0, ge=0)  # 태그당 주당 평균 Bad 구간 수
    bad_duration: tuple[timedelta, timedelta] = (timedelta(seconds=30), timedelta(seconds=400))

    @field_validator("bad_duration", mode="before")
    @classmethod
    def _durations(cls, v):
        return tuple(parse_duration(x) if isinstance(x, str) else x for x in v)


class ScenarioConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[ScenarioEvent] = []
    maintenance: list[MaintenanceEvent] = []
    random_anomalies: RandomAnomalies = RandomAnomalies()
    random_quality: RandomQuality = RandomQuality()

    def validate_against(self, plant: PlantConfig) -> None:
        """시나리오에 적힌 ID가 구성안에 없거나 겹치면 어느 ID 인지 알려 주며 멈춘다(기획서 3.4).

        통과하면 비어 있던 equip_id(이벤트·related)를 채워 이후 코드가 (equip_id, tag_id) 키를 바로 쓴다.
        """
        equips = set(plant.equipment_by_id())
        problems: list[str] = []
        for e in self.events:
            if e.equip_id is not None and e.equip_id not in equips:
                problems.append(f"event {e.event_id}: equipment {e.equip_id}")
                continue
            if e.tag_id is not None:
                try:
                    e.equip_id, e.tag_id = _resolve_strict(plant, e.tag_id, e.equip_id)
                except ConfigError as err:
                    problems.append(f"event {e.event_id}: {err}")
            for r in e.related:
                try:
                    if r.equip_id is not None:                          # 직접 적은 설비는 그대로 존중한다
                        r.equip_id, r.tag_id = _resolve_strict(plant, r.tag_id, r.equip_id)
                    else:                                               # 안 적었으면 이벤트 설비에서 먼저, 없으면 공장 안에서 유일할 때
                        r.equip_id, r.tag_id = plant.resolve(r.tag_id, equip_hint=e.equip_id)
                except ConfigError as err:
                    problems.append(f"event {e.event_id}: related {err}")
        problems += [f"maintenance {m.work_order_id}: equipment {m.equip_id}" for m in self.maintenance if m.equip_id not in equips]
        if problems:
            raise ConfigError("scenario references unknown IDs: " + "; ".join(problems))


def _resolve_strict(plant: PlantConfig, ref: str, equip_id: str | None) -> TagKey:
    """equip_id 를 직접 적었으면 그 설비의 태그여야 한다. 다른 설비로 조용히 옮기지 않는다."""
    key = plant.resolve(ref, equip_hint=equip_id)
    if equip_id is not None and key[0] != equip_id:
        raise ConfigError(f"equipment {equip_id} has no tag {ref} (found on {key[0]}); fix equip_id or write EQUIP/TAG")
    return key


def load_scenario(path: str | Path, plant: PlantConfig) -> ScenarioConfig:
    cfg = ScenarioConfig.model_validate(load_yaml(path))
    cfg.validate_against(plant)
    return cfg
