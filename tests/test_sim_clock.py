"""시뮬레이터 시계 (기획서 7.4): 모든 구성요소는 컴퓨터 시계가 아니라 이 시계를 본다."""
import asyncio
import time
from datetime import datetime, timedelta

from common.sim_clock import KST, SimClock, align_down, floor_to_interval


def test_align_down_to_interval():
    t = datetime(2026, 11, 8, 17, 23, 17, tzinfo=KST)
    assert align_down(t, 10) == datetime(2026, 11, 8, 17, 23, 10, tzinfo=KST)
    assert floor_to_interval(t, 600) == datetime(2026, 11, 8, 17, 20, 0, tzinfo=KST)


async def test_backfill_clock_never_sleeps():
    start = datetime(2026, 8, 1, tzinfo=KST)
    clock = SimClock.backfill(start)
    t0 = time.monotonic()
    await clock.sleep_until(start + timedelta(days=30))
    assert time.monotonic() - t0 < 0.05
    assert clock.now() == start + timedelta(days=30)


async def test_accelerated_clock_runs_speed_times_faster():
    start = datetime(2026, 11, 8, tzinfo=KST)
    clock = SimClock.accelerated(start, speed=600)
    t0 = time.monotonic()
    await clock.sleep_until(start + timedelta(seconds=60))   # 60초 시뮬레이터 시간 = 0.1초 실제 시간
    elapsed = time.monotonic() - t0
    assert 0.08 <= elapsed < 0.5
    assert abs((clock.now() - (start + timedelta(seconds=60))).total_seconds()) < 0.5 * 600 / 60


async def test_live_clock_tracks_wall_time():
    clock = SimClock.live()
    now = clock.now()
    assert now.tzinfo is not None
    assert abs((now - datetime.now(KST)).total_seconds()) < 1
    await clock.sleep_until(now)   # 과거 시각은 즉시 반환
