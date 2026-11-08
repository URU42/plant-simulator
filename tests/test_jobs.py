"""집계·알람·보관 배치 (기획서 7.3): 측정 시각 기준으로 구간을 나누고, 심각도는 팀 규칙대로."""
from datetime import datetime, timedelta, timezone

import pytest

from collector.jobs import JobRunner
from collector.seed_master import seed_master
from collector.storage import Storage
from common.config_models import RuntimeConfig, load_plant
from common.timefmt import to_db

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(plant_path):
    plant = load_plant(plant_path)
    storage = Storage(":memory:")
    seed_master(storage, plant)
    return storage, plant, JobRunner(storage, plant, RuntimeConfig())


def _feed(storage, tag_id, equip_id, start, minutes, value, batch="b"):
    """start 부터 minutes 분 동안 10초 간격으로 같은 값을 넣는다."""
    rows = [(tag_id, to_db(start + timedelta(seconds=s)), equip_id, value, "Good") for s in range(0, minutes * 60, 10)]
    storage.store_batch(f"{batch}-{tag_id}-{start.isoformat()}", "SIM", to_db(start), rows)


def test_10m_aggregate_uses_measurement_time(env):
    storage, plant, jobs = env
    _feed(storage, "VI-C1101", "C-1101", T0, 25, 2.0)
    jobs.advance(storage.sim_now())
    rows = storage.query_dicts("SELECT * FROM ts_10m WHERE tag_id = 'VI-C1101' ORDER BY bucket_start")
    assert [r["bucket_start"] for r in rows] == [to_db(T0), to_db(T0 + timedelta(minutes=10))]   # 20~25분 구간은 아직 미완료
    assert rows[0]["count"] == 60 and rows[0]["avg"] == 2.0 and rows[0]["status"] == "Good"


def test_alarm_severity_follows_team_rule(env):
    storage, plant, jobs = env
    # VI-C1101: warn 4.5 / alarm 6.0 / trip 7.1, delay_samples 2 → 연속 10분 구간 2개 초과해야 알람
    _feed(storage, "VI-C1101", "C-1101", T0, 10, 5.0)                          # 구간 1: warn 초과 (1회)
    _feed(storage, "VI-C1101", "C-1101", T0 + timedelta(minutes=10), 10, 5.0)  # 구간 2: warn 초과 (2회 → 알람)
    _feed(storage, "VI-C1101", "C-1101", T0 + timedelta(minutes=20), 10, 7.5)  # 구간 3: trip 초과 (1회)
    _feed(storage, "VI-C1101", "C-1101", T0 + timedelta(minutes=30), 10, 7.5)  # 구간 4: trip 초과 (2회 → 알람)
    _feed(storage, "VI-C1101", "C-1101", T0 + timedelta(minutes=40), 10, 2.0)  # 구간 5: 정상 → 해제
    _feed(storage, "VI-C1101", "C-1101", T0 + timedelta(minutes=50), 10, 2.0)  # 구간 6: 마감용
    jobs.advance(storage.sim_now())

    alarms = storage.query_dicts("SELECT * FROM alarm_detail ORDER BY occurred_at, limit_type")
    by_type = {a["limit_type"]: a for a in alarms}
    assert set(by_type) == {"Soft", "Alarm", "Trip"}
    assert by_type["Soft"]["severity"] == "Low" and by_type["Soft"]["occurred_at"] == to_db(T0 + timedelta(minutes=10))
    assert by_type["Alarm"]["severity"] == "Medium"
    assert by_type["Trip"]["severity"] == "High" and by_type["Trip"]["occurred_at"] == to_db(T0 + timedelta(minutes=30))
    assert by_type["Trip"]["limit_value"] == 7.1 and by_type["Trip"]["actual_value"] == 7.5
    assert abs(by_type["Trip"]["exceed_pct"] - (7.5 - 7.1) / 7.1 * 100) < 1e-6
    assert all(a["cleared_at"] == to_db(T0 + timedelta(minutes=40)) for a in alarms)


def test_single_exceeding_window_below_delay_does_not_alarm(env):
    storage, plant, jobs = env
    _feed(storage, "VI-C1101", "C-1101", T0, 10, 5.0)
    _feed(storage, "VI-C1101", "C-1101", T0 + timedelta(minutes=10), 10, 2.0)
    _feed(storage, "VI-C1101", "C-1101", T0 + timedelta(minutes=20), 10, 2.0)
    jobs.advance(storage.sim_now())
    assert storage.count("alarm_detail") == 0


def test_pressure_delay_one_alarms_immediately(env):
    storage, plant, jobs = env
    _feed(storage, "PI-C1101", "C-1101", T0, 10, 56.0)       # warn 55, delay_samples 1
    _feed(storage, "PI-C1101", "C-1101", T0 + timedelta(minutes=10), 10, 48.0)
    jobs.advance(storage.sim_now())
    rows = storage.query_dicts("SELECT * FROM alarm_detail")
    assert len(rows) == 1 and rows[0]["limit_type"] == "Soft" and rows[0]["occurred_at"] == to_db(T0)


def test_hourly_aggregate_and_nodata_status(env):
    storage, plant, jobs = env
    _feed(storage, "VI-C1101", "C-1101", T0, 70, 2.0)
    jobs.advance(storage.sim_now())
    h = storage.query_dicts("SELECT * FROM ts_1h WHERE tag_id = 'VI-C1101'")
    assert len(h) == 1 and h[0]["count"] == 360 and h[0]["status"] == "Good"
    # 같은 시간대에 값이 없는 다른 태그는 NoData 로 기록된다
    other = storage.query_dicts("SELECT status, count FROM ts_10m WHERE tag_id = 'TI-C1101'")
    assert other and all(r["status"] == "NoData" and r["count"] == 0 for r in other)


def test_daily_retention_deletes_old_raw(env):
    storage, plant, jobs = env
    old = T0 - timedelta(days=9)
    _feed(storage, "VI-C1101", "C-1101", old, 10, 2.0)
    _feed(storage, "VI-C1101", "C-1101", T0, 10, 2.0)
    _feed(storage, "VI-C1101", "C-1101", T0 + timedelta(days=1), 10, 2.0)   # 하루 경계를 넘겨 매일 배치가 돌게 한다
    jobs.advance(storage.sim_now())
    remaining = storage.query("SELECT MIN(ts) FROM ts_raw")[0][0]
    assert remaining == to_db(T0)
    assert storage.query("SELECT COUNT(*) FROM ts_10m WHERE bucket_start < ?", (to_db(T0),))[0][0] > 0   # 집계는 남는다


def test_advance_is_idempotent(env):
    storage, plant, jobs = env
    _feed(storage, "VI-C1101", "C-1101", T0, 30, 5.0)
    jobs.advance(storage.sim_now())
    n10, na = storage.count("ts_10m"), storage.count("alarm_detail")
    jobs.advance(storage.sim_now())
    assert (storage.count("ts_10m"), storage.count("alarm_detail")) == (n10, na)
