"""메시지 형식 v1 (기획서 6장): 직렬화·검증."""
import json

import pytest
from pydantic import ValidationError

from common.message import SCHEMA_VERSION, IngestBatch, Sample, make_batch_id


def test_sample_roundtrip():
    s = Sample(equip_id="C-1101", tag_id="VI-C1101", ts="2026-11-08T17:23:10+09:00", value=2.34, quality="Good")
    assert json.loads(s.model_dump_json())["value"] == 2.34
    assert s.ts.utcoffset().total_seconds() == 9 * 3600


def test_bad_quality_allows_null_value():
    s = Sample(equip_id="P-1101", tag_id="TI-P1101", ts="2026-11-08T17:23:10+09:00", value=None, quality="ScanOff")
    assert s.value is None


def test_good_quality_requires_value():
    with pytest.raises(ValidationError):
        Sample(equip_id="P-1101", tag_id="TI-P1101", ts="2026-11-08T17:23:10+09:00", value=None, quality="Good")


def test_unknown_quality_rejected():
    with pytest.raises(ValidationError):
        Sample(equip_id="P-1101", tag_id="TI-P1101", ts="2026-11-08T17:23:10+09:00", value=1.0, quality="Weird")


def test_naive_timestamp_rejected():
    with pytest.raises(ValidationError):
        Sample(equip_id="P-1101", tag_id="TI-P1101", ts="2026-11-08T17:23:10", value=1.0, quality="Good")


def test_batch_serializes_spec_fields_only():
    batch = IngestBatch(
        source_id="SIM-U1",
        batch_id="b-20261108-172400-SIM-U1-0001",
        sent_at="2026-11-08T17:24:00+09:00",
        samples=[Sample(equip_id="C-1101", tag_id="VI-C1101", ts="2026-11-08T17:23:10+09:00", value=2.34, quality="Good")],
    )
    payload = json.loads(batch.model_dump_json())
    assert set(payload) == {"schema_version", "source_id", "batch_id", "sent_at", "samples"}
    assert payload["schema_version"] == SCHEMA_VERSION == "1.0"
    assert set(payload["samples"][0]) == {"equip_id", "tag_id", "ts", "value", "quality"}
    assert payload["samples"][0]["ts"] == "2026-11-08T17:23:10+09:00"


def test_batch_id_format():
    from datetime import datetime, timedelta, timezone

    kst = timezone(timedelta(hours=9))
    assert make_batch_id(datetime(2026, 11, 8, 17, 24, tzinfo=kst), "SIM-U1", 1) == "b-20261108-172400-SIM-U1-0001"


def test_uncertain_quality_requires_value():
    from datetime import datetime, timezone
    Sample(equip_id="E", tag_id="T", ts=datetime(2026, 1, 1, tzinfo=timezone.utc), value=1.0, quality="Uncertain")
    with pytest.raises(ValidationError):
        Sample(equip_id="E", tag_id="T", ts=datetime(2026, 1, 1, tzinfo=timezone.utc), value=None, quality="Uncertain")
