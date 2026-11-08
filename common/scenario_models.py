"""시나리오 파일 모델 (기획서 5.4).

scenario_dev.yaml(개발용)과 scenario_eval.yaml(평가용, 비공개)이 같은 형식을 쓴다.
평가용 전용 항목(trend, question_aliases)은 선택 필드로 두어 같은 로더로 읽는다.
"""
from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from common.config_models import ConfigError, PlantConfig, load_yaml

_DURATION = re.compile(r"^(\d+)([smhd])$")
_UNIT_SEC = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> timedelta:
    """'90s' / '30m' / '12h' / '5d' 형식."""
    m = _DURATION.match(str(text).strip())
    if not m:
        raise ValueError(f"invalid duration {text!r} (use e.g. 30m, 12h, 5d)")
    return timedelta(seconds=int(m.group(1)) * _UNIT_SEC[m.group(2)])


class RelatedTag(BaseModel):
    """함께 움직일 태그와 배율."""

    model_config = ConfigDict(extra="forbid")
    tag_id: str
    ratio: float


class ScenarioEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    pattern: str                       # simulator.patterns.PATTERNS 의 이름
    start: AwareDatetime
    duration: timedelta
    tag_id: str | None = None          # 둘 중 하나. equip_id 만 주면 그 설비의 모든 태그에 적용
    equip_id: str | None = None
    magnitude: float = 0.0             # 값 패턴의 크기(태그 단위). freeze/quality/gap 은 쓰지 않는다
    quality: str = "Bad"               # quality 패턴일 때의 품질 (Bad / ScanOff)
    related: list[RelatedTag] = []
    spread_to_driven: float | None = None   # driven_by 로 이어진 피구동 설비 진동 태그에 번지는 비율
    # 연결 정보
    failure_id: str | None = None
    work_order_id: str | None = None
    doc_id: str | None = None
    recovered_at: AwareDatetime | None = None   # 정비 후 정상 복귀 시각. 없으면 start + duration 에 끝난다
    description: str | None = None
    # 평가용 파일에만
    trend: str | None = None                    # 정답 추이 방향: 상승 / 하강 / 안정
    question_aliases: list[str] = []            # 질문에 쓸 별칭

    @field_validator("duration", mode="before")
    @classmethod
    def _duration(cls, v):
        return parse_duration(v) if isinstance(v, str) else v

    @model_validator(mode="after")
    def _target(self) -> "ScenarioEvent":
        if (self.tag_id is None) == (self.equip_id is None):
            raise ValueError(f"event {self.event_id}: give exactly one of tag_id / equip_id")
        if self.recovered_at is not None and self.recovered_at < self.start + self.duration:
            raise ValueError(f"event {self.event_id}: recovered_at must be after start + duration")
        return self

    @property
    def end(self):
        """패턴이 적용되는 구간의 끝. 정비 복귀 시각이 있으면 그때까지 마지막 값을 유지한다."""
        return self.recovered_at or (self.start + self.duration)


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


class ScenarioConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[ScenarioEvent] = []
    maintenance: list[MaintenanceEvent] = []
    random_anomalies: RandomAnomalies = RandomAnomalies()

    def validate_against(self, plant: PlantConfig) -> None:
        """시나리오에 적힌 ID가 구성안에 없으면 어느 ID가 빠졌는지 알려 주며 멈춘다(기획서 3.4)."""
        tags, equips = set(plant.tag_by_id()), set(plant.equipment_by_id())
        missing: list[str] = []
        for e in self.events:
            if e.tag_id and e.tag_id not in tags:
                missing.append(f"event {e.event_id}: tag {e.tag_id}")
            if e.equip_id and e.equip_id not in equips:
                missing.append(f"event {e.event_id}: equipment {e.equip_id}")
            missing += [f"event {e.event_id}: related tag {r.tag_id}" for r in e.related if r.tag_id not in tags]
        missing += [f"maintenance {m.work_order_id}: equipment {m.equip_id}" for m in self.maintenance if m.equip_id not in equips]
        if missing:
            raise ConfigError("scenario references unknown IDs: " + "; ".join(missing))


def load_scenario(path: str | Path, plant: PlantConfig) -> ScenarioConfig:
    cfg = ScenarioConfig.model_validate(load_yaml(path))
    cfg.validate_against(plant)
    return cfg
