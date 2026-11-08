"""저장용 시각 문자열.

DB에는 모든 시각을 UTC ISO 문자열(예: 2026-11-08T08:23:20+00:00)로 넣는다.
자릿수가 고정되어 문자열 비교가 시각 비교와 같고, 입력은 오프셋이 있는 어떤 ISO 시각이든 받는다.
"""
from __future__ import annotations

from datetime import datetime, timezone


def to_db(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def from_db(s: str) -> datetime:
    return datetime.fromisoformat(s)


def parse_iso(s: str) -> datetime:
    """API 파라미터용. 오프셋이 없으면 UTC로 본다."""
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
