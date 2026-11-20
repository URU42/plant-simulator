"""메시지 형식 v3 (기획서 6장).

시뮬레이터가 POST /ingest 로 보내는 묶음. 센서가 실제로 알 수 있는 값(설비·태그·측정 시각·값)만 담고,
단위·한계값·이상 여부·시나리오 ID는 일부러 싣지 않는다(6.1).
v3 에서 status(Good/Bad)를 뺐다 (사용자 확정 2026-11-19: 설비 상태는 플랜트가 아니라 탐지·ML 이 판단한다).
v3.x 에서는 필드 추가만 허용한다(6.3).
"""
from __future__ import annotations

from datetime import datetime
from pydantic import AwareDatetime, BaseModel, ConfigDict

SCHEMA_VERSION = "3.0"


class Sample(BaseModel):
    """측정값 한 건. 값은 늘 있다."""

    model_config = ConfigDict(extra="forbid")

    equip_id: str
    tag_id: str
    ts: AwareDatetime           # 측정 시각. 가속 모드에서는 시뮬레이터 시계 기준
    value: float


class IngestBatch(BaseModel):
    """POST /ingest 본문."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    source_id: str
    batch_id: str               # 같은 묶음이 두 번 오면 한 번만 저장하는 기준
    sent_at: AwareDatetime
    samples: list[Sample]


def make_batch_id(sent_at: datetime, source_id: str, seq: int) -> str:
    """batch_id 규칙: b-{YYYYmmdd-HHMMSS}-{source_id}-{일련번호 4자리}."""
    return f"b-{sent_at:%Y%m%d-%H%M%S}-{source_id}-{seq:04d}"
