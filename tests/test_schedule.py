from datetime import date, datetime
from common.sim_clock import KST
from simulator.schedule import daily_events

D = date(2026, 11, 12)

def test_daily_events_shape():
    evs = daily_events(7, "C-3101", D)
    day0 = datetime(2026, 11, 12, tzinfo=KST).timestamp()
    assert len(evs) == 12
    assert sum(e.end - e.start for e in evs) == 8122
    assert all(day0 <= e.start < e.end <= day0 + 86400 for e in evs)
    assert all(a.end <= b.start for a, b in zip(evs, evs[1:]))
    assert {e.rule for e in evs} == set(range(1, 9))
    assert all(e.event_id.startswith("SP-C-3101-20261112-") for e in evs)
    assert all(not e.normal_change or e.rule == 2 for e in evs)

def test_daily_events_deterministic_and_distinct():
    assert daily_events(7, "C-3101", D) == daily_events(7, "C-3101", D)
    assert daily_events(7, "C-3101", D) != daily_events(7, "P-3101", D)
