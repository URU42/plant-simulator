"""통합 실행 (k8s 설계 §4.1): 수집 서버 프로세스 안에서 공장별 생성기를 돌린다.

HTTP 대신 LocalSender 가 그 공장의 PlantContext.receive 를 부른다. 값 계산과 저장·배치 전진은 워커 스레드에서 돌고
(이벤트 루프는 비어 있다), 백필 창은 10분이며 동시에 저장하는 공장 수를 EMBEDDED_CONCURRENCY 로 제한한다.
그래서 첫 백필 중에도 조회 API 와 프로브가 응답한다. 재시작하면 그 공장 DB 의 태그별 최근 값으로 생성기를 되살린다.
생성기는 그 공장의 사건 장부(ctx.book)에서 주입·지속 고장을 읽고, 실시간 구간이 시계를 따라잡으면 ctx.set_live 로 알린다.
"""
from __future__ import annotations

import asyncio
import logging

from collector.plant_context import PlantContext
from collector.plants import PlantEntry
from collector.settings import Settings
from simulator.main import run_mode
from simulator.sender import LocalSender

log = logging.getLogger("collector.embedded")

EMBEDDED_BACKFILL_WINDOW_SEC = 600     # 1초 공장 약 4만 샘플 ≈ 창당 0.5초 이하
EMBEDDED_CONCURRENCY = 2               # 동시에 저장(수신)하는 공장 수
# GIL 전환 간격(초). 생성기는 순수 파이썬 반복이라 워커 스레드가 GIL 을 오래 쥔다. 기본 5ms 면 조회 API 한 번이
# 스레드를 오가며 1초 가까이 걸려(실측 0.9초), 1ms 로 줄여 0.2초 안쪽으로 둔다
EMBEDDED_SWITCH_INTERVAL_SEC = 0.001


async def run_plant_simulator(ctx: PlantContext, entry: PlantEntry, settings: Settings,
                              limiter: asyncio.Semaphore | None = None) -> None:
    """공장 하나의 생성기를 time_mode 대로 끝없이 돌린다."""
    runtime = ctx.runtime
    overrides = {k: v for k, v in {"time_mode": settings.time_mode, "backfill_days": settings.backfill_days}.items() if v is not None}
    if overrides:
        runtime = runtime.model_validate(runtime.model_dump() | overrides)
    log.info("plant %s: embedded simulator %s", entry.key, runtime.time_mode)
    sender = LocalSender(ctx.receive_local, ctx.storage.sim_now, limiter, recent_values=ctx.storage.recent_values)
    await run_mode(ctx.plant, runtime, sender, source=ctx.book, window_sec=EMBEDDED_BACKFILL_WINDOW_SEC,
                   on_live=lambda t: ctx.set_live(t.timestamp()))
