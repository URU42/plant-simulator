"""POST /ingest 처리 규칙 (기획서 6.2).

- 같은 batch_id 가 다시 오면 저장하지 않고 duplicate 로 응답한다.
- 모르는 태그 ID나 형식이 틀린 행은 거절하고 건수와 이유를 돌려준다.
- 같은 (tag_id, ts) 는 한 번만 저장한다. 시각 순서가 뒤바뀐 행도 받는다.
"""
from __future__ import annotations

from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

from collector.storage import Storage
from common.message import SCHEMA_VERSION, Sample
from common.timefmt import to_db


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


def validate_samples(samples: list[Any], tag_owner: dict[str, str],
                     oldest_allowed: str | None = None) -> tuple[list[Sample], list[Rejection]]:
    """행 단위 검증. oldest_allowed(UTC ISO) 보다 오래된 측정 시각은 어차피 보관 기간 밖이므로 거절한다."""
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
        owner = tag_owner.get(s.tag_id)
        if owner is None:
            rejected.append(Rejection(index=i, tag_id=s.tag_id, reason=f"unknown tag_id {s.tag_id}"))
        elif owner != s.equip_id:
            rejected.append(Rejection(index=i, tag_id=s.tag_id, reason=f"equip_id {s.equip_id} does not own {s.tag_id}"))
        elif oldest_allowed is not None and to_db(s.ts) < oldest_allowed:
            rejected.append(Rejection(index=i, tag_id=s.tag_id, reason=f"ts {s.ts.isoformat()} is older than raw retention"))
        else:
            accepted.append(s)
    return accepted, rejected


def ingest(storage: Storage, batch: RawBatch, received_at: str, tag_owner: dict[str, str],
           oldest_allowed: str | None = None) -> tuple[IngestResult, list[Sample]]:
    """묶음을 저장하고 (결과, 받아들인 샘플) 을 돌려준다."""
    accepted, rejected = validate_samples(batch.samples, tag_owner, oldest_allowed)
    if storage.batch_seen(batch.batch_id):
        return IngestResult(batch_id=batch.batch_id, duplicate=True, accepted=len(accepted), stored=0,
                            rejected=len(rejected), reasons=rejected), []
    rows = [(s.tag_id, to_db(s.ts), s.equip_id, s.value, s.quality) for s in accepted]
    stored = storage.store_batch(batch.batch_id, batch.source_id, received_at, rows)
    return IngestResult(batch_id=batch.batch_id, duplicate=False, accepted=len(accepted), stored=stored,
                        rejected=len(rejected), reasons=rejected), accepted
