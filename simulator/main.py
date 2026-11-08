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
                 window_sec: int | None = None, catch_up_windows: int = 0) -> None:
        self.runtime = runtime
        self.sender = sender
        self.clock = clock
        self.window = timedelta(seconds=window_sec or runtime.send_interval_sec)
        self.generator = SampleGenerator(plant, runtime, scenario)
        self.seq = 0
        # 다음에 만들 구간의 시작. 재시작 때는 catch_up_windows 만큼 되감아 죽어 있던 구간을 다시 보낸다(수집 서버가 중복은 버린다)
        self.cursor: datetime = align_down(clock.now(), runtime.send_interval_sec) - self.window * catch_up_windows

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


def backfill_range(runtime: RuntimeConfig, now: datetime) -> tuple[datetime, datetime]:
    """백필 구간 [start, live_start). live_start 는 전송 주기에 맞춰 내림한 현재 시각."""
    live_start = align_down(now, runtime.send_interval_sec)
    return live_start - timedelta(days=runtime.backfill_days), live_start


async def run_backfill_then_live(plant: PlantConfig, runtime: RuntimeConfig, scenario, sender: Sender,
                                 now: datetime | None = None, live_clock: SimClock | None = None,
                                 live_until: datetime | None = None) -> None:
    """지난 backfill_days 를 1시간 묶음으로 빠르게 보낸 뒤, 끝난 구간부터 실시간으로 이어 간다.

    수집 서버에 이미 값이 있으면(재시작) 그 마지막 측정 시각의 구간부터 이어서 보내 90일을 다시 보내지 않는다.
    now / live_clock / live_until 은 테스트용이다. 실제 실행에서는 현재 시각과 실제 시계를 쓴다.
    """
    start, live_start = backfill_range(runtime, now or datetime.now(KST))
    resume_from = await sender.collector_time()
    if resume_from is not None:     # 수집 서버의 마지막 샘플 다음 샘플이 속한 구간부터
        next_sample = resume_from.astimezone(KST) + timedelta(seconds=runtime.sample_interval_sec)
        start = max(start, align_down(next_sample, runtime.send_interval_sec))
    backfill = Simulator(plant, runtime, scenario, sender, SimClock.backfill(start), window_sec=BACKFILL_WINDOW_SEC)
    if start < live_start:
        log.info("backfill %s → %s", start, live_start)
        await backfill.run(until=live_start)

    live = Simulator(plant, runtime, scenario, sender, live_clock or SimClock.live())
    live.cursor, live.seq = live_start, backfill.seq     # 백필이 끝난 구간부터 같은 일련번호로 이어 간다
    log.info("switch to live from %s", live_start)
    await live.run(until=live_until)


async def run_mode(plant: PlantConfig, runtime: RuntimeConfig, scenario, sender: Sender) -> None:
    """time_mode 에 따라 시계를 고르고 실행한다."""
    if runtime.time_mode == "backfill":
        await run_backfill_then_live(plant, runtime, scenario, sender)
    elif runtime.time_mode == "accelerated":
        start = align_down(datetime.now(KST), runtime.send_interval_sec)
        await Simulator(plant, runtime, scenario, sender, SimClock.accelerated(start, runtime.speed)).run()
    else:
        await Simulator(plant, runtime, scenario, sender, SimClock.live(), catch_up_windows=1).run()


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="설비 시뮬레이터")
    p.add_argument("--plant", default=os.environ.get("PLANT_CONFIG", "config/plant_hds.yaml"))
    p.add_argument("--runtime", default=os.environ.get("RUNTIME_CONFIG", "config/runtime.yaml"))
    p.add_argument("--scenario", action="append", default=None,
                   help="시나리오 파일. 여러 번 주면 모두 적용한다 (예: 개발용 + 비공개 평가용)")
    p.add_argument("--time-mode", choices=["backfill", "live", "accelerated"], help="runtime.yaml 의 time_mode 를 덮어쓴다")
    p.add_argument("--collector-url", default=os.environ.get("COLLECTOR_URL"), help="runtime.yaml 의 collector_url 을 덮어쓴다")
    return p.parse_args(argv)


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv)
    plant = load_plant(args.plant)
    runtime = load_runtime(args.runtime)
    overrides = {k: v for k, v in {"time_mode": args.time_mode, "collector_url": args.collector_url}.items() if v}
    if overrides:
        runtime = runtime.model_validate(runtime.model_dump() | overrides)
    scenario_paths = args.scenario or os.environ.get("SCENARIO_CONFIG", "").split(",")
    scenario = None
    if any(scenario_paths):
        from simulator.scenario import load_scenarios
        scenario = load_scenarios([p for p in scenario_paths if p], plant, runtime)
    sender = HttpSender(runtime.collector_url)

    async def run_and_close() -> None:
        try:
            await run_mode(plant, runtime, scenario, sender)
        finally:
            await sender.close()

    try:
        asyncio.run(run_and_close())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
