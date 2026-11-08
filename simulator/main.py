"""설비 시뮬레이터 실행: backfill / live / accelerated (기획서 7.4).

실행: python -m simulator.main --plant config/plant_hds.yaml --runtime config/runtime.yaml --scenario config/scenario_dev.yaml
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import datetime, timedelta

from common.config_models import PlantConfig, RuntimeConfig, load_plant, load_runtime
from common.message import IngestBatch, make_batch_id
from common.sim_clock import KST, SimClock, align_down
from simulator.generator import SampleGenerator
from simulator.sender import HttpSender, Sender

log = logging.getLogger("simulator")

BACKFILL_WINDOW_SEC = 3600      # 백필은 1시간 묶음으로 보내 HTTP 횟수를 줄인다


class Simulator:
    """시뮬레이터 시계를 따라 구간마다 샘플을 만들어 묶음으로 보낸다."""

    def __init__(self, plant: PlantConfig, runtime: RuntimeConfig, scenario, sender: Sender, clock: SimClock,
                 window_sec: int | None = None) -> None:
        self.runtime = runtime
        self.sender = sender
        self.clock = clock
        self.window = timedelta(seconds=window_sec or runtime.send_interval_sec)
        self.generator = SampleGenerator(plant, runtime, scenario)
        self.seq = 0
        self.cursor: datetime = align_down(clock.now(), runtime.send_interval_sec)   # 다음에 만들 구간의 시작

    async def run(self, until: datetime | None = None) -> None:
        """until 까지(없으면 끝없이) 구간을 만들어 보낸다. 구간이 끝나는 시각까지 기다렸다가 보낸다."""
        while until is None or self.cursor < until:
            end = self.cursor + self.window
            if until is not None:
                end = min(end, until)
            await self.clock.sleep_until(end)
            await self._send_window(self.cursor, end)
            self.cursor = end

    async def _send_window(self, start: datetime, end: datetime) -> None:
        samples = self.generator.generate(start, end)
        self.seq += 1
        sent_at = self.clock.now()
        batch = IngestBatch(source_id=self.runtime.source_id, batch_id=make_batch_id(end, self.runtime.source_id, self.seq),
                            sent_at=sent_at, samples=samples)
        result = await self.sender.send(batch)
        log.info("sent %s samples=%d stored=%s rejected=%s", batch.batch_id, len(samples),
                 result.get("stored"), result.get("rejected"))


async def run_mode(plant: PlantConfig, runtime: RuntimeConfig, scenario, sender: Sender) -> None:
    """time_mode 에 따라 시계를 고르고 실행한다. backfill 은 끝난 뒤 실시간으로 이어진다."""
    interval = runtime.send_interval_sec
    if runtime.time_mode == "backfill":
        live_start = align_down(datetime.now(KST), interval)
        start = live_start - timedelta(days=runtime.backfill_days)
        log.info("backfill %s → %s", start, live_start)
        sim = Simulator(plant, runtime, scenario, sender, SimClock.backfill(start), window_sec=BACKFILL_WINDOW_SEC)
        await sim.run(until=live_start)
        # 실시간 전환: 백필이 끝난 구간부터 같은 일련번호로 이어 간다
        live = Simulator(plant, runtime, scenario, sender, SimClock.live())
        live.cursor, live.seq = live_start, sim.seq
        log.info("switch to live from %s", live_start)
        await live.run()
    elif runtime.time_mode == "accelerated":
        start = align_down(datetime.now(KST), interval)
        await Simulator(plant, runtime, scenario, sender, SimClock.accelerated(start, runtime.speed)).run()
    else:
        await Simulator(plant, runtime, scenario, sender, SimClock.live()).run()


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="설비 시뮬레이터")
    p.add_argument("--plant", default=os.environ.get("PLANT_CONFIG", "config/plant_hds.yaml"))
    p.add_argument("--runtime", default=os.environ.get("RUNTIME_CONFIG", "config/runtime.yaml"))
    p.add_argument("--scenario", action="append", default=None,
                   help="시나리오 파일. 여러 번 주면 모두 적용한다 (예: 개발용 + 비공개 평가용)")
    p.add_argument("--time-mode", choices=["backfill", "live", "accelerated"], help="runtime.yaml 의 time_mode 를 덮어쓴다")
    return p.parse_args(argv)


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv)
    plant = load_plant(args.plant)
    runtime = load_runtime(args.runtime)
    if args.time_mode:
        runtime = runtime.model_copy(update={"time_mode": args.time_mode})
    scenario_paths = args.scenario or os.environ.get("SCENARIO_CONFIG", "").split(",")
    scenario = None
    if any(scenario_paths):
        from simulator.scenario import load_scenarios
        scenario = load_scenarios([p for p in scenario_paths if p], plant, runtime)
    sender = HttpSender(runtime.collector_url)
    try:
        asyncio.run(run_mode(plant, runtime, scenario, sender))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
