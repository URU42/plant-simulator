"""SSE 브로드캐스터 (기획서 2장, 6.4 GET /stream).

새 값이 들어올 때마다 연결된 모든 구독자 큐에 이벤트를 넣는다. 느린 구독자는 오래된 이벤트부터 버린다.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

QUEUE_SIZE = 256


class Broadcaster:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def publish(self, event: str, data: Any) -> None:
        message = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        for q in list(self._subscribers):
            if q.full():
                q.get_nowait()
            q.put_nowait(message)

    async def subscribe(self) -> AsyncIterator[str]:
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._subscribers.add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subscribers.discard(q)
