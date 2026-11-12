"""시뮬레이터 실행 루프 (기획서 9.2 5단계): 1분 묶음, 빈 구간 없이 이어진다."""
from datetime import datetime, timedelta

from common.config_models import RuntimeConfig, load_plant
from common.sim_clock import KST, SimClock
from simulator.main import Simulator
from simulator.sender import MemorySender

START = datetime(2026, 11, 8, 17, 20, tzinfo=KST)


def _runtime(**kw) -> RuntimeConfig:
    """RHDS 태그 주기(1초·10초)에 맞춘 실행 설정. 묶음은 1분."""
    return RuntimeConfig(sample_interval_sec=1, send_interval_sec=60, **kw)


async def test_batches_cover_window_without_gaps(plant_path):
    plant = load_plant(plant_path)
    runtime = _runtime(units=["UNIT-RHDS-01"], source_id="SIM-U1")
    sender = MemorySender()
    sim = Simulator(plant, runtime, sender=sender, clock=SimClock.backfill(START))
    await sim.run(until=START + timedelta(minutes=3))

    assert len(sender.batches) == 3
    assert [b.batch_id for b in sender.batches] == [
        "b-20261108-172100-SIM-U1-0001", "b-20261108-172200-SIM-U1-0002", "b-20261108-172300-SIM-U1-0003"]
    for b in sender.batches:
        assert b.source_id == "SIM-U1"
        assert len(b.samples) == 5 * 60 + 6       # P-3101 의 1초 태그 5개 × 60 + 10초 태그 1개 × 6
    ts = sorted({s.ts for b in sender.batches for s in b.samples})
    assert ts[0] == START and ts[-1] == START + timedelta(minutes=3) - timedelta(seconds=1)
    assert all((b - a).total_seconds() == 1 for a, b in zip(ts, ts[1:]))
    assert {s.equip_id for b in sender.batches for s in b.samples} == {"P-3101"}


async def test_split_units_produce_same_values_as_all(plant_path):
    plant = load_plant(plant_path)
    whole = MemorySender()
    await Simulator(plant, _runtime(), whole, SimClock.backfill(START)).run(until=START + timedelta(minutes=1))
    part = MemorySender()
    await Simulator(plant, _runtime(units=["UNIT-RHDS-03"], source_id="SIM-U3"), part, SimClock.backfill(START)).run(
        until=START + timedelta(minutes=1))
    whole_u3 = {(s.tag_id, s.ts): (s.value, s.quality) for s in whole.batches[0].samples if s.equip_id == "C-3101"}
    part_u3 = {(s.tag_id, s.ts): (s.value, s.quality) for s in part.batches[0].samples}
    assert whole_u3 == part_u3 and len(part_u3) == 7 * 60 + 4 * 6


async def test_live_restart_rewinds_one_window_so_no_minute_is_lost(plant_path):
    plant = load_plant(plant_path)
    sim = Simulator(plant, _runtime(units=["UNIT-RHDS-05"]), MemorySender(), SimClock.backfill(START + timedelta(seconds=37)),
                    catch_up_windows=1)
    assert sim.cursor == START - timedelta(minutes=1)
