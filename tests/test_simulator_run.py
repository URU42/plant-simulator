"""시뮬레이터 실행 루프 (기획서 9.2 5단계): 1분 묶음, 빈 구간 없이 이어진다."""
from datetime import datetime, timedelta

from common.config_models import RuntimeConfig, load_plant
from common.sim_clock import KST, SimClock
from simulator.main import Simulator
from simulator.sender import MemorySender

START = datetime(2026, 11, 8, 17, 20, tzinfo=KST)


async def test_batches_cover_window_without_gaps(plant_path):
    plant = load_plant(plant_path)
    runtime = RuntimeConfig(units=["U-1"], source_id="SIM-U1")
    sender = MemorySender()
    sim = Simulator(plant, runtime, scenario=None, sender=sender, clock=SimClock.backfill(START))
    await sim.run(until=START + timedelta(minutes=3))

    assert len(sender.batches) == 3
    assert [b.batch_id for b in sender.batches] == [
        "b-20261108-172100-SIM-U1-0001", "b-20261108-172200-SIM-U1-0002", "b-20261108-172300-SIM-U1-0003"]
    for b in sender.batches:
        assert b.source_id == "SIM-U1"
        assert len(b.samples) == 9 * 6            # U-1 태그 9개 × 10초 샘플 6개
    ts = sorted({s.ts for b in sender.batches for s in b.samples})
    assert ts[0] == START and ts[-1] == START + timedelta(minutes=3) - timedelta(seconds=10)
    assert all((b - a).total_seconds() == 10 for a, b in zip(ts, ts[1:]))
    assert {s.equip_id for b in sender.batches for s in b.samples} == {"C-1101", "ST-1101", "P-1101", "F-1101"}


async def test_split_units_produce_same_values_as_all(plant_path):
    plant = load_plant(plant_path)
    whole = MemorySender()
    await Simulator(plant, RuntimeConfig(), None, whole, SimClock.backfill(START)).run(until=START + timedelta(minutes=1))
    part = MemorySender()
    await Simulator(plant, RuntimeConfig(units=["U-3"], source_id="SIM-U3"), None, part, SimClock.backfill(START)).run(
        until=START + timedelta(minutes=1))
    whole_u3 = {(s.tag_id, s.ts): s.value for s in whole.batches[0].samples if s.tag_id.endswith("3101") or s.tag_id.endswith("3102")}
    part_u3 = {(s.tag_id, s.ts): s.value for s in part.batches[0].samples}
    assert whole_u3 == part_u3 and len(part_u3) == 10 * 6
