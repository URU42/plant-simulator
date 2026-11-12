"""수신 핵심 PlantContext.receive (k8s 설계 §4.1): HTTP 라우트와 LocalSender 가 함께 쓴다."""
from datetime import datetime, timedelta, timezone

import pytest

from collector.plant_context import PlantContext
from collector.plants import PlantEntry
from common.message import Sample
from common.timefmt import to_db

T0 = datetime(2026, 11, 9, 1, 0, tzinfo=timezone.utc)


@pytest.fixture
def ctx(rfcc_plant_path, rfcc_runtime_path):
    c = PlantContext.open(PlantEntry(key="rfcc", plant=str(rfcc_plant_path), runtime=str(rfcc_runtime_path), db=":memory:"))
    yield c
    c.close()


def _samples(start, n, value=1.45):
    return [Sample(equip_id="P-1101A", tag_id="VI-P-1101A", ts=start + timedelta(seconds=i), value=value, quality="Good") for i in range(n)]


def test_receive_stores_dedupes_and_rejects_old_rows(ctx):
    now = to_db(T0)
    r = ctx.receive("b-1", "T", _samples(T0, 5), now)
    assert (r.stored, r.accepted, r.duplicate) == (5, 5, False)
    assert ctx.receive("b-1", "T", _samples(T0, 5), now).duplicate is True
    ctx.receive("b-2", "T", _samples(T0 + timedelta(days=3), 1), now)          # sim_now 가 3일 앞으로
    r = ctx.receive("b-3", "T", _samples(T0 + timedelta(days=1), 2), now)      # 보관(1일) 밖
    assert r.stored == 0 and r.rejected == 2 and "retention" in r.reasons[0].reason


def test_receive_publishes_only_with_subscribers(ctx, monkeypatch):
    calls = []
    monkeypatch.setattr(ctx.broadcaster, "publish", lambda *a: calls.append(a))
    ctx.receive("b-1", "T", _samples(T0, 3), to_db(T0))
    assert calls == []                                                           # 구독자 없음
    monkeypatch.setattr(ctx.broadcaster, "has_subscribers", lambda: True)
    ctx.receive("b-2", "T", _samples(T0 + timedelta(seconds=10), 3), to_db(T0))
    assert len(calls) == 1 and len(calls[0][1]) == 3


def test_storage_recent_values_are_last_n_per_tag_oldest_first(ctx):
    ctx.receive("b-1", "T", _samples(T0, 5, 1.0) + [Sample(equip_id="P-1101A", tag_id="TI-P-1101A", ts=T0, value=65.0, quality="Bad")],
                to_db(T0))
    ctx.receive("b-2", "T", [Sample(equip_id="P-1101A", tag_id="VI-P-1101A", ts=T0 + timedelta(seconds=5 + i), value=2.0 + i,
                                    quality="Good") for i in range(2)], to_db(T0))
    assert ctx.storage.recent_values(3) == {("P-1101A", "VI-P-1101A"): [1.0, 2.0, 3.0], ("P-1101A", "TI-P-1101A"): [65.0]}
    assert ctx.storage.recent_values(14)[("P-1101A", "VI-P-1101A")] == [1.0] * 5 + [2.0, 3.0]
