"""집계·알람·보관 배치 (기획서 7.3): 측정 시각 기준으로 구간을 나누고, 심각도는 팀 규칙대로."""
from datetime import datetime, timedelta, timezone

import pytest

from collector.jobs import JobRunner
from collector.seed_master import seed_master
from collector.storage import Storage
from common.config_models import Limits, RuntimeConfig, load_plant
from common.timefmt import to_db

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(plant_path):
    plant = load_plant(plant_path)
    storage = Storage(":memory:")
    seed_master(storage, plant)
    return storage, plant, JobRunner(storage, plant, RuntimeConfig())


def _feed(storage, equip_id, tag_id, start, minutes, value, batch="b"):
    """start 부터 minutes 분 동안 10초 간격으로 같은 값을 넣는다. 행은 (equip_id, tag_id, ts, value, quality)."""
    rows = [(equip_id, tag_id, to_db(start + timedelta(seconds=s)), value, "Good") for s in range(0, minutes * 60, 10)]
    storage.store_batch(f"{batch}-{tag_id}-{start.isoformat()}", "SIM", to_db(start), rows)


def test_10m_aggregate_uses_measurement_time(env):
    storage, plant, jobs = env
    _feed(storage, "C-1101", "VI-C1101", T0, 25, 2.0)
    jobs.advance(storage.sim_now())
    rows = storage.query_dicts("SELECT * FROM ts_10m WHERE tag_id = 'VI-C1101' ORDER BY bucket_start")
    assert [r["bucket_start"] for r in rows] == [to_db(T0), to_db(T0 + timedelta(minutes=10))]   # 20~25분 구간은 아직 미완료
    assert rows[0]["count"] == 60 and rows[0]["avg"] == 2.0 and rows[0]["status"] == "Good"


def test_alarm_severity_follows_team_rule(env):
    storage, plant, jobs = env
    # VI-C1101: warn 4.5 / alarm 6.0 / trip 7.1, delay_samples 2 → 연속 10분 구간 2개 초과해야 알람
    _feed(storage, "C-1101", "VI-C1101", T0, 10, 5.0)                          # 구간 1: warn 초과 (1회)
    _feed(storage, "C-1101", "VI-C1101", T0 + timedelta(minutes=10), 10, 5.0)  # 구간 2: warn 초과 (2회 → 알람)
    _feed(storage, "C-1101", "VI-C1101", T0 + timedelta(minutes=20), 10, 7.5)  # 구간 3: trip 초과 (1회)
    _feed(storage, "C-1101", "VI-C1101", T0 + timedelta(minutes=30), 10, 7.5)  # 구간 4: trip 초과 (2회 → 알람)
    _feed(storage, "C-1101", "VI-C1101", T0 + timedelta(minutes=40), 10, 2.0)  # 구간 5: 정상 → 해제
    _feed(storage, "C-1101", "VI-C1101", T0 + timedelta(minutes=50), 10, 2.0)  # 구간 6: 마감용
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
    _feed(storage, "C-1101", "VI-C1101", T0, 10, 5.0)
    _feed(storage, "C-1101", "VI-C1101", T0 + timedelta(minutes=10), 10, 2.0)
    _feed(storage, "C-1101", "VI-C1101", T0 + timedelta(minutes=20), 10, 2.0)
    jobs.advance(storage.sim_now())
    assert storage.count("alarm_detail") == 0


def test_pressure_delay_one_alarms_immediately(env):
    storage, plant, jobs = env
    _feed(storage, "C-1101", "PI-C1101", T0, 10, 56.0)       # warn 55, delay_samples 1
    _feed(storage, "C-1101", "PI-C1101", T0 + timedelta(minutes=10), 10, 48.0)
    jobs.advance(storage.sim_now())
    rows = storage.query_dicts("SELECT * FROM alarm_detail")
    assert len(rows) == 1 and rows[0]["limit_type"] == "Soft" and rows[0]["occurred_at"] == to_db(T0)


def test_hourly_aggregate_and_nodata_status(env):
    storage, plant, jobs = env
    _feed(storage, "C-1101", "VI-C1101", T0, 70, 2.0)
    jobs.advance(storage.sim_now())
    h = storage.query_dicts("SELECT * FROM ts_1h WHERE tag_id = 'VI-C1101'")
    assert len(h) == 1 and h[0]["count"] == 360 and h[0]["status"] == "Good"
    # 같은 시간대에 값이 없는 다른 태그는 NoData 로 기록된다
    other = storage.query_dicts("SELECT status, count FROM ts_10m WHERE tag_id = 'TI-C1101'")
    assert other and all(r["status"] == "NoData" and r["count"] == 0 for r in other)


def test_retention_runs_with_ten_minute_batch(env):
    """보관 삭제는 10분 배치마다. 하루 보관이면 T0−1d+5m 행은 하루 경계(매일 배치)로는 남지만 10분 배치가 지운다."""
    storage, plant, _ = env
    jobs = JobRunner(storage, plant, RuntimeConfig(raw_retention_days=1))
    old = T0 - timedelta(days=1) + timedelta(minutes=5)
    _feed(storage, "C-1101", "VI-C1101", old, 5, 2.0)        # T0−1d+5m ~ T0−1d+10m: 마지막 구간 끝(T0+10m)−1d 보다 오래됨
    _feed(storage, "C-1101", "VI-C1101", T0, 10, 2.0)
    _feed(storage, "C-1101", "VI-C1101", T0 + timedelta(minutes=10), 1, 2.0)   # sim_now = T0+10m
    while jobs.advance(storage.sim_now()):
        pass
    assert storage.query("SELECT MIN(ts) FROM ts_raw")[0][0] == to_db(T0)
    assert storage.query("SELECT COUNT(*) FROM ts_10m WHERE bucket_start < ?", (to_db(T0),))[0][0] > 0   # 집계는 남는다


def test_stale_daily_job_state_is_harmless(env):
    storage, plant, jobs = env
    storage.execute("INSERT INTO job_state VALUES ('daily', ?)", (to_db(T0),))
    _feed(storage, "C-1101", "VI-C1101", T0, 10, 2.0)
    _feed(storage, "C-1101", "VI-C1101", T0 + timedelta(minutes=10), 1, 2.0)
    assert jobs.advance(storage.sim_now()) >= 1


def test_advance_is_idempotent(env):
    storage, plant, jobs = env
    _feed(storage, "C-1101", "VI-C1101", T0, 30, 5.0)
    jobs.advance(storage.sim_now())
    n10, na = storage.count("ts_10m"), storage.count("alarm_detail")
    jobs.advance(storage.sim_now())
    assert (storage.count("ts_10m"), storage.count("alarm_detail")) == (n10, na)


def test_advance_processes_at_most_a_cap_per_call_and_reports_progress(env):
    """오래 멈췄다 켜져도 한 번에 다 처리하지 않는다. 남은 구간은 다음 호출이 이어서 처리한다."""
    from collector.jobs import MAX_WINDOWS_PER_ADVANCE

    storage, plant, jobs = env
    _feed(storage, "C-1101", "VI-C1101", T0, 10, 2.0)
    _feed(storage, "C-1101", "VI-C1101", T0 + timedelta(days=3), 10, 2.0)      # sim_now 가 3일 건너뛴다
    first = jobs.advance(storage.sim_now())
    assert first == MAX_WINDOWS_PER_ADVANCE
    assert storage.query("SELECT COUNT(DISTINCT bucket_start) FROM ts_10m")[0][0] == MAX_WINDOWS_PER_ADVANCE
    total = first
    while (n := jobs.advance(storage.sim_now())) > 0:
        total += n
    assert storage.query("SELECT COUNT(DISTINCT bucket_start) FROM ts_10m")[0][0] == 3 * 144    # 마지막 10분 구간은 아직 미완료
    assert storage.query("SELECT COUNT(DISTINCT bucket_start) FROM ts_1h")[0][0] == 3 * 24
    assert jobs.advance(storage.sim_now()) == 0


# ---------------------------------------------------------------------------
# 하한 알람 · Uncertain (다중 공장 설계 §4.2, §4.3)
# ---------------------------------------------------------------------------

def test_lo_limit_alarm_uses_window_min(env):
    """하한 알람: 10분 최소값이 warn_lo 아래면 Low, direction lo."""
    storage, plant, _ = env
    plant = plant.model_copy(deep=True)
    tag = plant.tag_by_key()[("C-1101", "PI-C1101")]
    tag.limits = Limits(warn_lo=40.0, alarm_lo=35.0, warn_hi=55, alarm_hi=58, trip_hi=60)
    jobs = JobRunner(storage, plant, RuntimeConfig())
    _feed(storage, "C-1101", "PI-C1101", T0, 10, 38.0)
    _feed(storage, "C-1101", "PI-C1101", T0 + timedelta(minutes=10), 1, 48.0)
    jobs.advance(storage.sim_now())
    rows = storage.query_dicts("SELECT limit_type, direction, severity, limit_value, actual_value, cleared_at FROM alarm_detail")
    assert [(r["limit_type"], r["direction"], r["severity"], r["limit_value"], r["actual_value"]) for r in rows] == [("Soft", "lo", "Low", 40.0, 38.0)]
    _feed(storage, "C-1101", "PI-C1101", T0 + timedelta(minutes=10), 10, 48.0)
    _feed(storage, "C-1101", "PI-C1101", T0 + timedelta(minutes=20), 1, 48.0)
    jobs.advance(storage.sim_now())
    assert storage.query("SELECT cleared_at FROM alarm_detail")[0][0] is not None


def test_lo_and_hi_alarms_are_independent(env):
    storage, plant, _ = env
    plant = plant.model_copy(deep=True)
    plant.tag_by_key()[("C-1101", "PI-C1101")].limits = Limits(warn_lo=40.0, warn_hi=55.0)
    jobs = JobRunner(storage, plant, RuntimeConfig())
    rows = [("C-1101", "PI-C1101", to_db(T0 + timedelta(seconds=s)), 30.0 if s < 300 else 60.0, "Good") for s in range(0, 600, 10)]
    storage.store_batch("b-both", "SIM", to_db(T0), rows)                     # 한 구간에서 하한·상한 모두 넘는다
    _feed(storage, "C-1101", "PI-C1101", T0 + timedelta(minutes=10), 1, 50.0)
    jobs.advance(storage.sim_now())
    assert sorted(r[0] for r in storage.query("SELECT direction FROM alarm_detail WHERE cleared_at IS NULL")) == ["hi", "lo"]


def test_uncertain_counts_as_value(env):
    storage, plant, jobs = env
    rows = [("C-1101", "VI-C1101", to_db(T0 + timedelta(seconds=s)), 2.0, "Uncertain" if s % 20 else "Good") for s in range(0, 600, 10)]
    storage.store_batch("b-unc", "SIM", to_db(T0), rows)
    _feed(storage, "C-1101", "VI-C1101", T0 + timedelta(minutes=10), 1, 2.0)
    jobs.advance(storage.sim_now())
    r = storage.query_dicts("SELECT avg, count, status FROM ts_10m WHERE tag_id = 'VI-C1101' AND bucket_start = ?", (to_db(T0),))[0]
    assert r["count"] == 60 and abs(r["avg"] - 2.0) < 1e-9 and r["status"] == "Good"
