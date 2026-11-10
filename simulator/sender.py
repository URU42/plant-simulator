"""전송부 (교체 가능). HTTP POST /ingest 가 기본이고, 통합 실행에서는 같은 프로세스의 수집 함수를 바로 부른다(LocalSender)."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Callable, Protocol

import httpx

from common.message import IngestBatch, Sample
from common.timefmt import from_db

log = logging.getLogger(__name__)

MAX_BACKOFF_SEC = 60.0


class SendError(Exception):
    """재시도를 다 써도 묶음을 보내지 못했다."""


class Sender(Protocol):
    async def send(self, batch: IngestBatch) -> dict[str, Any]: ...

    async def collector_time(self) -> datetime | None:
        """수집 서버가 가진 마지막 측정 시각. 백필을 이어서 할 때 쓴다. 모르면 None."""
        ...

    async def close(self) -> None: ...


class HttpSender:
    """httpx 로 POST /ingest.

    실패하면 backoff(상한 60초)를 두고 다시 보낸다. retries 가 None(기본)이면 포기하지 않는다:
    값은 시각의 순수 함수라 수집 서버가 살아나면 밀린 구간을 그대로 이어 보낼 수 있기 때문이다.
    """

    def __init__(self, collector_url: str, retries: int | None = None, backoff_sec: float = 2.0,
                 timeout_sec: float = 30.0, transport: httpx.AsyncBaseTransport | None = None,
                 ingest_token: str | None = None) -> None:
        self._base = collector_url.rstrip("/")
        self._retries = retries
        self._backoff = backoff_sec
        self._client = httpx.AsyncClient(timeout=timeout_sec, transport=transport)
        self._headers = {"content-type": "application/json"}
        token = ingest_token if ingest_token is not None else os.environ.get("INGEST_TOKEN")
        if token:                                   # 수집 서버가 토큰을 요구하면 같은 헤더를 붙인다
            self._headers["X-Ingest-Token"] = token

    async def send(self, batch: IngestBatch) -> dict[str, Any]:
        body = batch.model_dump_json()
        attempt = 0
        while True:
            attempt += 1
            try:
                r = await self._client.post(self._base + "/ingest", content=body, headers=self._headers)
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, ValueError) as e:
                if self._retries is not None and attempt >= self._retries:
                    raise SendError(f"giving up on {batch.batch_id}: {e}") from e
                log.warning("send %s failed (attempt %d): %s", batch.batch_id, attempt, e)
                await asyncio.sleep(min(self._backoff * attempt, MAX_BACKOFF_SEC))

    async def collector_time(self) -> datetime | None:
        try:
            r = await self._client.get(self._base + "/health")
            r.raise_for_status()
            sim_now = r.json().get("sim_now")
        except (httpx.HTTPError, ValueError) as e:
            log.warning("collector time unavailable: %s", e)
            return None
        return from_db(sim_now) if sim_now else None

    async def close(self) -> None:
        await self._client.aclose()


class LocalSender:
    """같은 프로세스의 수집 함수를 바로 부른다 (통합 실행). JSON 직렬화·재검증 없이 Sample 을 그대로 넘긴다."""

    def __init__(self, receive: Callable[[str, str, list[Sample]], Any], sim_now: Callable[[], str | None],
                 limiter: asyncio.Semaphore | None = None) -> None:
        self._receive = receive
        self._sim_now = sim_now
        self._limiter = limiter          # 여러 공장이 동시에 백필할 때 한꺼번에 저장하는 수를 제한한다 (메모리 피크·GIL 경합)

    async def send(self, batch: IngestBatch) -> dict[str, Any]:
        # 저장·배치 전진은 워커 스레드에서. 백필처럼 기다림 없는 구간에서도 이벤트 루프(조회 API·프로브)가 비어 있다
        if self._limiter is not None:
            async with self._limiter:
                r = await asyncio.to_thread(self._receive, batch.batch_id, batch.source_id, batch.samples)
        else:
            r = await asyncio.to_thread(self._receive, batch.batch_id, batch.source_id, batch.samples)
        return {"batch_id": r.batch_id, "stored": r.stored, "rejected": r.rejected}

    async def collector_time(self) -> datetime | None:
        sim_now = self._sim_now()
        return from_db(sim_now) if sim_now else None

    async def close(self) -> None:
        return None


class MemorySender:
    """테스트·검증용. 보낸 묶음을 메모리에 쌓아 둔다."""

    def __init__(self) -> None:
        self.batches: list[IngestBatch] = []

    async def send(self, batch: IngestBatch) -> dict[str, Any]:
        self.batches.append(batch)
        return {"batch_id": batch.batch_id, "stored": len(batch.samples)}

    async def collector_time(self) -> datetime | None:
        return None

    async def close(self) -> None:
        return None
