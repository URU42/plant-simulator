"""설비 시뮬레이터 실행: backfill / live / accelerated (기획서 7.4).

실행: python -m simulator.main --plant config/plant_rhds.yaml --runtime config/runtime_rhds.yaml
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Callable

from common.config_models import PlantConfig, RuntimeConfig, load_plant, load_runtime
from common.message import IngestBatch, make_batch_id
from common.nelson import HISTORY
from common.sim_clock import KST, SimClock, align_down
from simulator.nelson_model import EventSource, NelsonGenerator
from simulator.sender import HttpSender, Sender

log = logging.getLogger("simulator")

BACKFILL_WINDOW_SEC = 3600      # 백필은 1시간 묶음으로 보내 HTTP 횟수를 줄인다


class Simulator:
    """시뮬레이터 시계를 따라 구간마다 샘플을 만들어 묶음으로 보낸다."""

    def __init__(self, plant: PlantConfig, runtime: RuntimeConfig, sender: Sender, clock: SimClock,
                 source: EventSource | None = None, window_sec: int | None = None, catch_up_windows: int = 0) -> None:
        self.runtime = runtime
        self.sender = sender
        self.clock = clock
        self.window = timedelta(seconds=window_sec or runtime.send_interval_sec)
        self.generator = NelsonGenerator(plant, runtime, source)
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
            await asyncio.sleep(0)      # 백필처럼 기다림이 없는 시계에서도 같은 루프의 다른 작업(조회 API 등)에 양보한다

    async def _send_window(self, start: datetime, end: datetime) -> None:
        samples = await asyncio.to_thread(self.generator.generate, start, end)   # 값 계산은 루프 밖에서
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


async def restore_from_collector(sim: Simulator, sender: Sender) -> None:
    """수집 서버가 가진 태그별 최근 HISTORY 개 값으로 생성기의 판정 상태를 되살린다. 값이 없으면 그대로 둔다."""
    values = await sender.recent_values(HISTORY)
    if values:
        sim.generator.restore(values)


async def run_backfill_then_live(plant: PlantConfig, runtime: RuntimeConfig, sender: Sender,
                                 source: EventSource | None = None, now: datetime | None = None,
                                 live_clock: SimClock | None = None, live_until: datetime | None = None,
                                 window_sec: int = BACKFILL_WINDOW_SEC,
                                 on_live: Callable[[datetime], None] | None = None) -> None:
    """지난 backfill_days 를 1시간 묶음으로 빠르게 보낸 뒤, 끝난 구간부터 실시간으로 이어 간다.

    수집 서버에 이미 값이 있으면(재시작) 그 마지막 측정 시각의 구간부터 이어서 보내 90일을 다시 보내지 않고,
    그 최근 값으로 생성기를 되살린다. 실시간으로 넘어가는 순간 on_live(실시간 시작 시각)를 한 번 부른다.
    now / live_clock / live_until 은 테스트용이다. 실제 실행에서는 현재 시각과 실제 시계를 쓴다.
    """
    start, live_start = backfill_range(runtime, now or datetime.now(KST))
    resume_from = await sender.collector_time()
    if resume_from is not None:     # 수집 서버의 마지막 샘플 다음 샘플이 속한 구간부터
        next_sample = resume_from.astimezone(KST) + timedelta(seconds=runtime.sample_interval_sec)
        start = max(start, align_down(next_sample, runtime.send_interval_sec))
    backfill = Simulator(plant, runtime, sender, SimClock.backfill(start), source, window_sec=window_sec)
    if resume_from is not None:
        await restore_from_collector(backfill, sender)
    if start < live_start:
        log.info("backfill %s → %s", start, live_start)
        await backfill.run(until=live_start)

    live = Simulator(plant, runtime, sender, live_clock or SimClock.live(), source)
    # 백필이 끝난 구간부터 같은 생성기(판정 이력·난수)와 같은 일련번호로 이어 간다
    live.generator, live.cursor, live.seq = backfill.generator, live_start, backfill.seq
    log.info("switch to live from %s", live_start)
    if on_live is not None:
        on_live(live_start)
    await live.run(until=live_until)


async def run_mode(plant: PlantConfig, runtime: RuntimeConfig, sender: Sender, source: EventSource | None = None,
                   window_sec: int = BACKFILL_WINDOW_SEC, on_live: Callable[[datetime], None] | None = None) -> None:
    """time_mode 에 따라 시계를 고르고 실행한다. window_sec 은 백필 묶음 길이.

    on_live(시각) 는 실시간(또는 가속)으로 도는 첫 시각에 한 번 불린다: backfill 은 실시간 전환 때, live·accelerated 는 시작 때.
    """
    if runtime.time_mode == "backfill":
        await run_backfill_then_live(plant, runtime, sender, source, window_sec=window_sec, on_live=on_live)
        return
    if runtime.time_mode == "accelerated":
        start = align_down(datetime.now(KST), runtime.send_interval_sec)
        sim = Simulator(plant, runtime, sender, SimClock.accelerated(start, runtime.speed), source)
    else:
        sim = Simulator(plant, runtime, sender, SimClock.live(), source, catch_up_windows=1)
    await restore_from_collector(sim, sender)
    if on_live is not None:
        on_live(sim.cursor)
    await sim.run()


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="설비 시뮬레이터")
    p.add_argument("--plant", default=os.environ.get("PLANT_CONFIG", "config/plant_rhds.yaml"))
    p.add_argument("--runtime", default=os.environ.get("RUNTIME_CONFIG", "config/runtime_rhds.yaml"))
    p.add_argument("--time-mode", choices=["backfill", "live", "accelerated"], help="실행 설정의 time_mode 를 덮어쓴다")
    p.add_argument("--collector-url", default=os.environ.get("COLLECTOR_URL"), help="실행 설정의 collector_url 을 덮어쓴다")
    return p.parse_args(argv)


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv)
    plant = load_plant(args.plant)
    runtime = load_runtime(args.runtime)
    overrides = {k: v for k, v in {"time_mode": args.time_mode, "collector_url": args.collector_url}.items() if v}
    if overrides:
        runtime = runtime.model_validate(runtime.model_dump() | overrides)
    sender = HttpSender(runtime.collector_url)

    async def run_and_close() -> None:
        try:
            await run_mode(plant, runtime, sender)
        finally:
            await sender.close()

    try:
        asyncio.run(run_and_close())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
