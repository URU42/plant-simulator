"""백필 → 실시간 전환, 가속 모드 (기획서 7.4, 9.2 8단계)."""
import time
from datetime import datetime, timedelta

from common.config_models import RuntimeConfig, load_plant
from common.sim_clock import KST, SimClock
from simulator.main import Simulator, backfill_range, run_backfill_then_live
from simulator.sender import MemorySender

NOW = datetime(2026, 11, 8, 12, 0, 37, tzinfo=KST)


def test_backfill_range_is_aligned_to_send_interval():
    start, live_start = backfill_range(RuntimeConfig(backfill_days=90), NOW)
    assert live_start == datetime(2026, 11, 8, 12, 0, tzinfo=KST)
    assert start == live_start - timedelta(days=90)


async def test_backfill_then_live_has_no_gap_and_is_reproducible(plant_path):
    plant = load_plant(plant_path)
    runtime = RuntimeConfig(units=["U-5"], source_id="SIM-U5", time_mode="backfill", backfill_days=1)

    async def run_once():
        sender = MemorySender()
        await run_backfill_then_live(plant, runtime, None, sender, now=NOW,
                                     live_clock=SimClock.backfill(NOW), live_until=NOW.replace(second=0) + timedelta(minutes=3))
        return sender

    a = await run_once()
    assert len(a.batches) == 24 + 3                       # 백필 1시간 묶음 24개 + 실시간 1분 묶음 3개
    assert [b.batch_id[-4:] for b in a.batches[-4:]] == ["0024", "0025", "0026", "0027"]
    ts = sorted({s.ts for b in a.batches for s in b.samples})
    assert ts[0] == NOW.replace(second=0) - timedelta(days=1)
    assert all((y - x).total_seconds() == 10 for x, y in zip(ts, ts[1:]))   # 백필 끝과 실시간 시작 사이 빈 구간 없음
    assert len(a.batches[24].samples) == 4 * 6                # U-5 태그 4개 × 6

    b = await run_once()
    assert [(s.tag_id, s.ts, s.value) for x in a.batches for s in x.samples] == \
           [(s.tag_id, s.ts, s.value) for x in b.batches for s in x.samples]


async def test_accelerated_mode_runs_faster_than_wall_clock(plant_path):
    plant = load_plant(plant_path)
    runtime = RuntimeConfig(units=["U-6"], source_id="SIM-U6", time_mode="accelerated", speed=1200)
    start = datetime(2026, 11, 8, 12, 0, tzinfo=KST)
    sender = MemorySender()
    t0 = time.monotonic()
    await Simulator(plant, runtime, None, sender, SimClock.accelerated(start, 1200)).run(until=start + timedelta(minutes=2))
    assert time.monotonic() - t0 < 1.0                        # 2분 시뮬레이터 시간 = 0.1초 실제 시간
    assert len(sender.batches) == 2 and sender.batches[0].samples[0].ts == start
