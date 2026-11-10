"""POST /ingest 의 행 단위 검증 (기획서 6.2).

- 모르는 (설비, 태그) 나 형식이 틀린 행은 거절하고 건수와 이유를 돌려준다.
- 저장·중복 확인·보관 기간 거절·배치 전진은 PlantContext.receive 가 한다 (HTTP 라우트와 로컬 전송부가 함께 쓴다).
"""
from __future__ import annotations

from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

from common.config_models import TagKey
from common.message import SCHEMA_VERSION, Sample


class RawSample(Sample):
    """수신 쪽 행 모델. v1.x 에서 추가된 필드는 무시하고 받는다(6.3). 시뮬레이터 쪽 Sample 은 오타를 잡기 위해 forbid."""

    model_config = ConfigDict(extra="ignore")


class RawBatch(BaseModel):
    """묶음 바깥 틀만 검증한다. samples 는 행 단위로 따로 검증해 일부만 거절할 수 있게 한다."""

    schema_version: str
    source_id: str
    batch_id: str
    sent_at: AwareDatetime
    samples: list[Any]


class Rejection(BaseModel):
    index: int
    tag_id: str | None
    reason: str


class IngestResult(BaseModel):
    batch_id: str
    duplicate: bool
    accepted: int
    stored: int
    rejected: int
    reasons: list[Rejection]


def schema_supported(version: str) -> bool:
    """v1.x 는 필드 추가만 있으므로 주 버전이 같으면 받는다(6.3)."""
    return version.split(".")[0] == SCHEMA_VERSION.split(".")[0]


def validate_samples(samples: list[Any], tag_keys: set[TagKey]) -> tuple[list[Sample], list[Rejection]]:
    """행 단위 검증: 형식과 (설비, 태그) 존재."""
    accepted: list[Sample] = []
    rejected: list[Rejection] = []
    for i, raw in enumerate(samples):
        tag_id = raw.get("tag_id") if isinstance(raw, dict) else None
        try:
            s = RawSample.model_validate(raw)
        except ValidationError as e:
            fields = ", ".join(str(err["loc"][0]) for err in e.errors() if err["loc"]) or "body"
            rejected.append(Rejection(index=i, tag_id=tag_id, reason=f"invalid {fields}"))
            continue
        if (s.equip_id, s.tag_id) not in tag_keys:
            rejected.append(Rejection(index=i, tag_id=s.tag_id, reason=f"unknown tag {s.equip_id}/{s.tag_id}"))
        else:
            accepted.append(s)
    return accepted, rejected

