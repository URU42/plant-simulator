"""백필 → 실시간 전환, 가속 모드, 재개 때 상태 되살리기 (기획서 7.4, 9.2 8단계)."""
import time
from datetime import datetime, timedelta

import pytest

from common.config_models import RuntimeConfig, load_plant
from common.nelson import HISTORY
from common.sim_clock import KST, SimClock
from simulator.main import Simulator, backfill_range, run_backfill_then_live, run_mode
from simulator.nelson_model import NelsonGenerator
from simulator.sender import MemorySender

NOW = datetime(2026, 11, 8, 12, 0, 37, tzinfo=KST)
LIVE_START = NOW.replace(second=0)


def _runtime(**kw) -> RuntimeConfig:
    """RHDS 한 Unit(P-3103: 1초 태그 4개 + 10초 태그 1개), 1분 묶음."""
    base = dict(units=["UNIT-RHDS-06"], source_id="SIM-U6", sample_interval_sec=1, send_interval_sec=60)
    return RuntimeConfig(**(base | kw))


class ResumingSender(MemorySender):
    """수집 서버에 이미 sim_now 까지, 태그별 최근 값 values 가 들어 있는 상황."""

    def __init__(self, sim_now, values=None):
        super().__init__()
        self.sim_now = sim_now
        self.values = values or {}
        self.asked: list[int] = []

    async def collector_time(self):
        return self.sim_now

    async def recent_values(self, count):
        self.asked.append(count)
        return self.values


def test_backfill_range_is_aligned_to_send_interval():
    start, live_start = backfill_range(RuntimeConfig(backfill_days=90), NOW)
    assert live_start == datetime(2026, 11, 8, 12, 0, tzinfo=KST)
    assert start == live_start - timedelta(days=90)


async def test_backfill_then_live_has_no_gap_and_is_reproducible(plant_path):
    """2시간 백필(1시간 묶음 2개) 뒤 실시간 1분 묶음 3개. 전환 때 생성기 상태를 이어 가므로 한 번에 만든 값과 같다."""
    plant = load_plant(plant_path)
    runtime = _runtime(time_mode="backfill", backfill_days=1)

    async def run_once():
        sender = ResumingSender(LIVE_START - timedelta(hours=2, seconds=1))     # 마지막 저장 샘플 = 09:59:59
        on_live = []
        await run_backfill_then_live(plant, runtime, sender, now=NOW, live_clock=SimClock.backfill(NOW),
                                     live_until=LIVE_START + timedelta(minutes=3), on_live=on_live.append)
        assert on_live == [LIVE_START]
        return sender

    a = await run_once()
    assert len(a.batches) == 2 + 3
    assert [b.batch_id[-4:] for b in a.batches[-4:]] == ["0002", "0003", "0004", "0005"]
    ts = sorted({s.ts for b in a.batches for s in b.samples})
    assert ts[0] == LIVE_START - timedelta(hours=2)
    assert all((y - x).total_seconds() == 1 for x, y in zip(ts, ts[1:]))   # 백필 끝과 실시간 시작 사이 빈 구간 없음
    assert len(a.batches[2].samples) == 4 * 60 + 6

    b = await run_once()
    values = [(s.tag_id, s.ts, s.value, s.quality) for x in a.batches for s in x.samples]
    assert values == [(s.tag_id, s.ts, s.value, s.quality) for x in b.batches for s in x.samples]

    whole = MemorySender()          # 같은 구간을 한 생성기로 쭉 만든 값
    await Simulator(plant, runtime, whole, SimClock.backfill(LIVE_START - timedelta(hours=2)), window_sec=3600).run(
        until=LIVE_START + timedelta(minutes=3))
    assert values == [(s.tag_id, s.ts, s.value, s.quality) for x in whole.batches for s in x.samples]


async def test_accelerated_mode_runs_faster_than_wall_clock(plant_path):
    plant = load_plant(plant_path)
    runtime = _runtime(time_mode="accelerated", speed=1200)
    start = datetime(2026, 11, 8, 12, 0, tzinfo=KST)
    sender = MemorySender()
    t0 = time.monotonic()
    await Simulator(plant, runtime, sender, SimClock.accelerated(start, 1200)).run(until=start + timedelta(minutes=2))
    assert time.monotonic() - t0 < 1.0                        # 2분 시뮬레이터 시간 = 0.1초 실제 시간
    assert len(sender.batches) == 2 and sender.batches[0].samples[0].ts == start


async def test_backfill_resumes_from_collector_time(plant_path):
    """재시작하면 수집 서버가 가진 마지막 측정 시각 다음부터 이어서 백필한다 (90일을 다시 보내지 않는다)."""
    plant = load_plant(plant_path)
    runtime = _runtime(time_mode="backfill", backfill_days=1)
    sender = ResumingSender(LIVE_START - timedelta(hours=2, seconds=1))
    await run_backfill_then_live(plant, runtime, sender, now=NOW, live_clock=SimClock.backfill(NOW),
                                 live_until=LIVE_START + timedelta(minutes=1))
    assert len(sender.batches) == 2 + 1
    assert sender.batches[0].samples[0].ts == LIVE_START - timedelta(hours=2)
    assert sender.batches[0].batch_id.endswith("-0001")

    far_future = ResumingSender(LIVE_START + timedelta(days=3))                 # 수집 서버가 앞서 있으면 백필은 건너뛴다
    on_live = []
    await run_backfill_then_live(plant, runtime, far_future, now=NOW, live_clock=SimClock.backfill(NOW),
                                 live_until=LIVE_START + timedelta(minutes=1), on_live=on_live.append)
    assert len(far_future.batches) == 1 and on_live == [LIVE_START]


async def test_resume_restores_generator_from_recent_values(plant_path, monkeypatch):
    """수집 서버에 값이 있으면 태그별 최근 HISTORY 개로 생성기를 되살린다. 값이 없으면 되살리지 않는다."""
    restored = []
    original = NelsonGenerator.restore
    monkeypatch.setattr(NelsonGenerator, "restore", lambda self, v: (restored.append(v), original(self, v)))
    plant = load_plant(plant_path)
    runtime = _runtime(time_mode="backfill", backfill_days=1)
    values = {("P-3103", "VI-A-P3103"): [1.0, 1.1, 1.2]}
    sender = ResumingSender(LIVE_START - timedelta(seconds=1), values)
    await run_backfill_then_live(plant, runtime, sender, now=NOW, live_clock=SimClock.backfill(NOW),
                                 live_until=LIVE_START + timedelta(minutes=1))
    assert sender.asked == [HISTORY] and restored == [values]

    restored.clear()
    empty = ResumingSender(LIVE_START - timedelta(seconds=1))
    await run_backfill_then_live(plant, runtime, empty, now=NOW, live_clock=SimClock.backfill(NOW),
                                 live_until=LIVE_START + timedelta(minutes=1))
    assert restored == []

    fresh = ResumingSender(None)                                                  # 수집 서버가 비어 있으면 묻지도 않는다
    await run_backfill_then_live(plant, _runtime(time_mode="backfill", backfill_days=0), fresh, now=NOW,
                                 live_clock=SimClock.backfill(NOW), live_until=LIVE_START + timedelta(minutes=1))
    assert fresh.asked == [] and restored == []


class _Stop(Exception):
    pass


class StopAfterFirst(MemorySender):
    """첫 묶음을 받고 실행을 끝낸다 (끝없이 도는 live·accelerated 를 시험하려고)."""

    async def send(self, batch):
        await super().send(batch)
        raise _Stop


@pytest.mark.parametrize("mode", ["live", "accelerated"])
async def test_run_mode_calls_on_live_once_at_first_cursor(plant_path, mode):
    plant = load_plant(plant_path)
    runtime = _runtime(time_mode=mode, speed=1200, send_interval_sec=1)
    sender, on_live = StopAfterFirst(), []
    with pytest.raises(_Stop):
        await run_mode(plant, runtime, sender, on_live=on_live.append)
    assert len(on_live) == 1 and on_live[0] == sender.batches[0].samples[0].ts
