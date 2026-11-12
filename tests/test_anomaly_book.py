"""사건 장부 (설계 §4.5–4.6): 주입·멈춤·지속 고장·정답지."""
import pytest
from collector.anomaly_book import AnomalyBook, Busy
from collector.storage import Storage
from collector.seed_master import seed_master
from common.config_models import load_plant
from simulator.anomaly import AnomalyEvent

@pytest.fixture
def book(config_dir, tmp_path):
    plant = load_plant(config_dir / "plant_rhds.yaml")
    st = Storage(tmp_path / "b.db"); seed_master(st, plant)
    return AnomalyBook(st, plant, seed=1, interval_min=15), st, plant

def test_inject_busy_and_stop(book):
    b, _, _ = book
    e = b.inject("C-3101", 1, at=1000.0)
    assert e.origin == "injected" and e.start == 1000.0 and e.end is None
    with pytest.raises(Busy):
        b.inject("C-3101", 2, at=1010.0)
    assert b.stop("C-3101", at=1100.0) == [e.event_id]
    assert b.stops()[e.event_id] == 1100.0

def test_stop_twice_and_empty(book):
    b, _, _ = book
    assert b.stop("C-3101", at=10.0) == [] and b.stop(None, at=10.0) == []
    e = b.inject("C-3101", 3, at=20.0)
    assert b.stop(None, at=30.0) == [e.event_id] and b.stop("C-3101", at=40.0) == []

def test_inject_truncates_active_spontaneous(book):
    b, _, _ = book
    sp = AnomalyEvent("SP-C-3101-20261112-01", "spontaneous", "C-3101", 2, 900.0, 1500.0)
    b.opened(sp)
    b.inject("C-3101", 1, at=1000.0)
    row = next(r for r in b.answer_key("C-3101", None, None) if r["event_id"] == sp.event_id)
    assert row["stop_requested_at"] is not None
    assert b.stops()[sp.event_id] == 1000.0

def test_persistent_only_after_live_every_interval(book):
    b, _, _ = book
    assert [e for e in b.external(0, 5000) if e.origin == "persistent"] == []
    b.set_live(10_000.0)
    assert [e for e in b.external(10_000, 10_899) if e.origin == "persistent"] == []
    first = [e for e in b.external(10_899, 10_901) if e.origin == "persistent"]
    assert len(first) == 1 and first[0].start == 10_900.0 and first[0].end is None
    assert len([e for e in b.external(11_799, 11_801) if e.origin == "persistent"]) == 2

def test_book_reload_keeps_open_events(book, config_dir):
    b, st, plant = book
    e = b.inject("C-3101", 5, at=50.0)
    again = AnomalyBook(st, plant, seed=1, interval_min=15)
    assert [x.event_id for x in again.external(0, 100)] == [e.event_id]

def test_answer_key_fields(book):
    b, _, _ = book
    e = b.inject("C-3101", 7, at=50.0); b.stop("C-3101", at=80.0)
    row = b.answer_key(None, None, None)[0]
    assert row["kind"] == "계측 이상" and row["response_tags"] == ["XI-X-C3101"] and row["end_at"] is not None
