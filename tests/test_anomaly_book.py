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
    assert b.stop("C-3101", at=1100.0) == (1100.0, [e.event_id])
    assert b.stops()[e.event_id] == 1100.0

def test_stop_twice_and_empty(book):
    b, _, _ = book
    assert b.stop("C-3101", at=10.0)[1] == [] and b.stop(None, at=10.0)[1] == []
    e = b.inject("C-3101", 3, at=20.0)
    assert b.stop(None, at=30.0)[1] == [e.event_id] and b.stop("C-3101", at=40.0)[1] == []

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


def test_inject_and_stop_never_land_in_generated_samples(book, config_dir):
    """생성기가 시계보다 늦게 재생 중이어도(요청 시각 < 이미 만든 구간 끝) 주입·멈춤은 이미 만든 표본 뒤로 미뤄진다."""
    from datetime import datetime, timedelta
    from common.config_models import load_runtime
    from common.sim_clock import KST
    from common.timefmt import to_db
    from simulator.anomaly import primary_tag
    from simulator.nelson_model import NelsonGenerator

    b, _, plant = book
    rt = load_runtime(config_dir / "runtime_rhds.yaml")
    rt = rt.model_copy(update={"units": [plant.equipment_by_id()["C-3101"].unit_id]})
    t0 = datetime(2026, 11, 12, 0, 0, tzinfo=KST)
    g = NelsonGenerator(plant, rt, b, spontaneous=False)
    first = g.generate(t0, t0 + timedelta(seconds=60))              # 장부의 high-water = t0 + 60
    high = t0.timestamp() + 60
    e = b.inject("C-3101", 1, at=t0.timestamp() + 10)              # 10초 시점 요청 (이미 만든 구간 안)
    row = b.answer_key("C-3101", None, None)[0]
    assert e.start == high and row["start_at"] == to_db(datetime.fromtimestamp(high, KST))
    second = g.generate(t0 + timedelta(seconds=60), t0 + timedelta(seconds=120))
    tag = primary_tag(plant, "C-3101")
    assert not [s for s in first if s.equip_id == "C-3101" and s.status == "Bad"]
    first_bad = next(s for s in second if s.tag_id == tag and s.status == "Bad")
    assert first_bad.ts.timestamp() == e.start
    at, ids = b.stop("C-3101", at=t0.timestamp() + 30)             # 멈춤도 같은 규칙 (high-water = t0 + 120)
    assert ids == [e.event_id] and at == t0.timestamp() + 120


def test_answer_key_skips_zero_length_event(book):
    """시작 시각에 멈춘 사건은 표본에 나온 적이 없으므로 정답지에 싣지 않는다."""
    b, _, _ = book
    e = b.inject("C-3101", 2, at=50.0)
    b.stop("C-3101", at=50.0)
    assert b.answer_key(None, None, None) == []
    assert e.event_id in b.stops()


def _capped(config_dir, tmp_path, cap):
    plant = load_plant(config_dir / "plant_rhds.yaml")
    st = Storage(tmp_path / "cap.db"); seed_master(st, plant)
    return AnomalyBook(st, plant, seed=1, interval_min=15, max_open=cap)

def _persistent(events):
    return [e for e in events if e.origin == "persistent"]

def test_persistent_cap_skips_slots_when_full(config_dir, tmp_path):
    b = _capped(config_dir, tmp_path, 2)
    L = 1_000_000.0
    b.set_live(L)
    opened = _persistent(b.external(L, L + 3 * 900 + 1))
    assert sorted(e.start for e in opened) == [L + 900, L + 1800]       # L+2700 회차는 건너뛴다
    b.stop(opened[0].equip_id, at=L + 3000)
    later = _persistent(b.external(L + 3 * 900 + 1, L + 4 * 900 + 1))
    assert [e.start for e in later if e.start > L + 2700] == [L + 3600]                       # 하나 멈추면 다음 회차가 열린다

def test_injected_events_do_not_count_toward_cap(config_dir, tmp_path):
    b = _capped(config_dir, tmp_path, 1)
    L = 1_000_000.0
    b.set_live(L)
    b.inject("C-3101", 1, at=L)
    assert len(_persistent(b.external(L, L + 901))) == 1
