"""전송부 (교체 가능). 지금은 HTTP POST /ingest, 나중에 MQTT로 바꿀 수 있게 인터페이스를 둔다."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

import httpx

from common.message import IngestBatch

log = logging.getLogger(__name__)


class SendError(Exception):
    """재시도를 다 써도 묶음을 보내지 못했다."""


class Sender(Protocol):
    async def send(self, batch: IngestBatch) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class HttpSender:
    """httpx 로 POST /ingest. 실패하면 backoff 를 두고 retries 번까지 다시 보낸다."""

    def __init__(self, collector_url: str, retries: int = 5, backoff_sec: float = 2.0,
                 timeout_sec: float = 30.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._url = collector_url.rstrip("/") + "/ingest"
        self._retries = retries
        self._backoff = backoff_sec
        self._client = httpx.AsyncClient(timeout=timeout_sec, transport=transport)

    async def send(self, batch: IngestBatch) -> dict[str, Any]:
        body = batch.model_dump_json()
        last_error: Exception | None = None
        for attempt in range(1, self._retries + 1):
            try:
                r = await self._client.post(self._url, content=body, headers={"content-type": "application/json"})
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, ValueError) as e:
                last_error = e
                log.warning("send %s failed (attempt %d/%d): %s", batch.batch_id, attempt, self._retries, e)
                if attempt < self._retries:
                    await asyncio.sleep(self._backoff * attempt)
        raise SendError(f"giving up on {batch.batch_id}: {last_error}")

    async def close(self) -> None:
        await self._client.aclose()


class MemorySender:
    """테스트·검증용. 보낸 묶음을 메모리에 쌓아 둔다."""

    def __init__(self) -> None:
        self.batches: list[IngestBatch] = []

    async def send(self, batch: IngestBatch) -> dict[str, Any]:
        self.batches.append(batch)
        return {"batch_id": batch.batch_id, "stored": len(batch.samples)}

    async def close(self) -> None:
        return None
