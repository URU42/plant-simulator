"""시뮬레이터 시계 (기획서 7.4).

모든 구성요소는 컴퓨터 시계가 아니라 이 시계를 본다.
- backfill: 잠들지 않고 요청한 시각으로 바로 건너뛴다.
- live: 실제 시계를 따른다.
- accelerated: 기준 시각부터 실제 경과 시간의 speed 배로 흐른다.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


def align_down(t: datetime, interval_sec: int) -> datetime:
    """t를 interval_sec 간격의 경계로 내림한다(초 단위, 자정 기준)."""
    sec = t.hour * 3600 + t.minute * 60 + t.second
    return t.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(seconds=sec - sec % interval_sec)


floor_to_interval = align_down


class SimClock:
    """now()와 sleep_until()만 제공한다. 생성은 backfill/live/accelerated 팩터리로 한다."""

    def __init__(self, mode: str, anchor: datetime, speed: float = 1.0) -> None:
        self.mode = mode
        self._anchor = anchor           # 시뮬레이터 시각 기준점
        self._speed = speed
        self._wall_anchor = time.monotonic()
        self._sim_now = anchor          # backfill 모드에서만 쓰는 현재 시각

    # --- 팩터리 ---------------------------------------------------------------
    @classmethod
    def backfill(cls, start: datetime) -> "SimClock":
        return cls("backfill", start)

    @classmethod
    def live(cls) -> "SimClock":
        return cls("live", datetime.now(KST))

    @classmethod
    def accelerated(cls, start: datetime, speed: float) -> "SimClock":
        return cls("accelerated", start, speed)

    # --- 시각 ---------------------------------------------------------------
    def now(self) -> datetime:
        if self.mode == "backfill":
            return self._sim_now
        if self.mode == "live":
            return datetime.now(KST)
        elapsed = time.monotonic() - self._wall_anchor
        return self._anchor + timedelta(seconds=elapsed * self._speed)

    async def sleep_until(self, t: datetime) -> None:
        """시뮬레이터 시각이 t가 될 때까지 기다린다. 이미 지났으면 바로 돌아온다."""
        if self.mode == "backfill":
            self._sim_now = max(self._sim_now, t)
            return
        remaining = (t - self.now()).total_seconds()
        if remaining > 0:
            await asyncio.sleep(remaining / self._speed)
