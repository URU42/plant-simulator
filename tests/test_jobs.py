"""집계·보관 배치 (기획서 7.3): 측정 시각 기준으로 구간을 나눈다."""
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


def _feed(storage, equip_id, tag_id, start, minutes, value, batch="b"):
    """start 부터 minutes 분 동안 10초 간격으로 같은 값을 넣는다. 행은 (equip_id, tag_id, ts, value, status)."""
    rows = [(equip_id, tag_id, to_db(start + timedelta(seconds=s)), value, "Good") for s in range(0, minutes * 60, 10)]
    storage.store_batch(f"{batch}-{tag_id}-{start.isoformat()}", "SIM", to_db(start), rows)


def test_10m_aggregate_uses_measurement_time(env):
    storage, plant, jobs = env
    _feed(storage, "P-3101", "VI-A-P3101", T0, 25, 2.0)
    jobs.advance(storage.sim_now())
    rows = storage.query_dicts("SELECT * FROM ts_10m WHERE tag_id = 'VI-A-P3101' ORDER BY bucket_start")
    assert [r["bucket_start"] for r in rows] == [to_db(T0), to_db(T0 + timedelta(minutes=10))]   # 20~25분 구간은 아직 미완료
    assert rows[0]["count"] == 60 and rows[0]["avg"] == 2.0 and rows[0]["status"] == "Good"


def test_hourly_aggregate_and_nodata_status(env):
    storage, plant, jobs = env
    _feed(storage, "P-3101", "VI-A-P3101", T0, 70, 2.0)
    jobs.advance(storage.sim_now())
    h = storage.query_dicts("SELECT * FROM ts_1h WHERE tag_id = 'VI-A-P3101'")
    assert len(h) == 1 and h[0]["count"] == 360 and h[0]["status"] == "Good"
    # 같은 시간대에 값이 없는 다른 태그는 NoData 로 기록된다
    other = storage.query_dicts("SELECT status, count FROM ts_10m WHERE tag_id = 'TI-B-P3101'")
    assert other and all(r["status"] == "NoData" and r["count"] == 0 for r in other)


def test_retention_runs_with_ten_minute_batch(env):
    """보관 삭제는 10분 배치마다. 하루 보관이면 T0−1d+5m 행은 하루 경계(매일 배치)로는 남지만 10분 배치가 지운다."""
    storage, plant, _ = env
    jobs = JobRunner(storage, plant, RuntimeConfig(raw_retention_days=1))
    old = T0 - timedelta(days=1) + timedelta(minutes=5)
    _feed(storage, "P-3101", "VI-A-P3101", old, 5, 2.0)        # T0−1d+5m ~ T0−1d+10m: 마지막 구간 끝(T0+10m)−1d 보다 오래됨
    _feed(storage, "P-3101", "VI-A-P3101", T0, 10, 2.0)
    _feed(storage, "P-3101", "VI-A-P3101", T0 + timedelta(minutes=10), 1, 2.0)   # sim_now = T0+10m
    while jobs.advance(storage.sim_now()):
        pass
    assert storage.query("SELECT MIN(ts) FROM ts_raw")[0][0] == to_db(T0)
    assert storage.query("SELECT COUNT(*) FROM ts_10m WHERE bucket_start < ?", (to_db(T0),))[0][0] > 0   # 집계는 남는다


def test_stale_daily_job_state_is_harmless(env):
    storage, plant, jobs = env
    storage.execute("INSERT INTO job_state VALUES ('daily', ?)", (to_db(T0),))
    _feed(storage, "P-3101", "VI-A-P3101", T0, 10, 2.0)
    _feed(storage, "P-3101", "VI-A-P3101", T0 + timedelta(minutes=10), 1, 2.0)
    assert jobs.advance(storage.sim_now()) >= 1


def test_advance_is_idempotent(env):
    storage, plant, jobs = env
    _feed(storage, "P-3101", "VI-A-P3101", T0, 30, 5.0)
    jobs.advance(storage.sim_now())
    n10, n1h = storage.count("ts_10m"), storage.count("ts_1h")
    jobs.advance(storage.sim_now())
    assert (storage.count("ts_10m"), storage.count("ts_1h")) == (n10, n1h)


def test_advance_processes_at_most_a_cap_per_call_and_reports_progress(env):
    """오래 멈췄다 켜져도 한 번에 다 처리하지 않는다. 남은 구간은 다음 호출이 이어서 처리한다."""
    from collector.jobs import MAX_WINDOWS_PER_ADVANCE

    storage, plant, jobs = env
    _feed(storage, "P-3101", "VI-A-P3101", T0, 10, 2.0)
    _feed(storage, "P-3101", "VI-A-P3101", T0 + timedelta(days=3), 10, 2.0)      # sim_now 가 3일 건너뛴다
    first = jobs.advance(storage.sim_now())
    assert first == MAX_WINDOWS_PER_ADVANCE
    assert storage.query("SELECT COUNT(DISTINCT bucket_start) FROM ts_10m")[0][0] == MAX_WINDOWS_PER_ADVANCE
    total = first
    while (n := jobs.advance(storage.sim_now())) > 0:
        total += n
    assert storage.query("SELECT COUNT(DISTINCT bucket_start) FROM ts_10m")[0][0] == 3 * 144    # 마지막 10분 구간은 아직 미완료
    assert storage.query("SELECT COUNT(DISTINCT bucket_start) FROM ts_1h")[0][0] == 3 * 24
    assert jobs.advance(storage.sim_now()) == 0


def test_limit_breach_makes_no_alarm(env):
    """한계값은 참고값이다. 10분 배치는 집계·보관만 하고 알람(nelson_alarm)을 만들지 않는다."""
    storage, plant, jobs = env
    _feed(storage, "P-3101", "VI-A-P3101", T0, 30, 12.0)                     # 경고 4.5 · 정지 11 위
    jobs.advance(storage.sim_now())
    assert storage.count("ts_10m") > 0 and storage.count("nelson_alarm") == 0




def test_bucket_status_bad_when_any_bad(env):
    """버킷 안에 Bad 표본이 하나라도 있으면 Bad, 평균·개수는 모든 표본으로 구한다."""
    storage, plant, jobs = env
    vals = [(33.1, "Good"), (33.2, "Good"), (40.0, "Bad")]
    storage.store_batch("b1", "T", to_db(T0), [("C-3101", "XI-X-C3101", to_db(T0 + timedelta(seconds=i)), v, s)
                                              for i, (v, s) in enumerate(vals)])
    jobs._aggregate("ts_10m", T0, T0 + timedelta(minutes=10))
    r = storage.query_dicts("SELECT status, count, avg FROM ts_10m WHERE tag_id = 'XI-X-C3101'")[0]
    assert (r["status"], r["count"]) == ("Bad", 3) and r["avg"] == pytest.approx((33.1 + 33.2 + 40.0) / 3)
