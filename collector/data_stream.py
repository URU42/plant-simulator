"""측정값을 Redis Stream 으로 보낸다 (사용자 확정 2026-11-19).

공장별 스트림 plant-sim:data:{공장 키} 에 초마다 메시지 하나를 XADD 한다. 필드 data 는 JSON
{plant, plant_id, ts, samples} (samples 는 그 초의 표본 [{equip_id, tag_id, ts, value}], 10초 태그는 그 초에만).
길이는 MAXLEN ~ 1800 (초당 1건 × 30분). 실제 시각 기준 최근 30분 안의 값만 보내 백필의 지난 값으로 스트림을 채우지 않는다.
보내기는 공장마다 스레드 하나가 큐에서 꺼내 파이프라인으로 한다 — Redis 가 느리거나 죽어도 수신·저장은 멈추지 않고,
큐가 차면 오래된 메시지부터 버리고 경고만 남긴다(1분에 한 번).
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from datetime import datetime, timedelta, timezone

import redis

log = logging.getLogger("collector.data_stream")

PREFIX = "plant-sim:data:"
KEEP = timedelta(minutes=30)
MAXLEN = int(KEEP.total_seconds())      # 초당 1건
TIMEOUT_SEC = 2.0
QUEUE_SIZE = MAXLEN                     # Redis 가 멈춰도 30분치만 들고 있는다
WARN_EVERY_SEC = 60


class DataStream:
    def __init__(self, client: redis.Redis, plant: str) -> None:
        self.client = client
        self.plant = plant
        self.name = PREFIX + plant
        self._q: queue.Queue[dict | None] = queue.Queue(maxsize=QUEUE_SIZE)
        self._warned = 0.0
        self._thread = threading.Thread(target=self._run, name=f"data-stream-{plant}", daemon=True)
        self._thread.start()

    @classmethod
    def from_url(cls, url: str, plant: str) -> "DataStream":
        """연결은 첫 송신 때 맺는다. 시작할 때 Redis 가 없어도 서버는 뜬다."""
        return cls(redis.Redis.from_url(url, socket_timeout=TIMEOUT_SEC, socket_connect_timeout=TIMEOUT_SEC), plant)

    def submit(self, messages: list[dict]) -> None:
        """보낼 메시지를 큐에 넣는다 (수신 경로에서 부른다, 막히지 않는다). 최근 30분 밖의 메시지는 버린다."""
        oldest = datetime.now(timezone.utc) - KEEP
        for m in messages:
            if datetime.fromisoformat(m["ts"]) < oldest:
                continue
            while True:
                try:
                    self._q.put_nowait(m)
                    break
                except queue.Full:
                    self._drop_oldest()

    def _drop_oldest(self) -> None:
        try:
            self._q.get_nowait()
        except queue.Empty:
            pass
        self._warn("queue full, dropping the oldest message")

    def _run(self) -> None:
        while True:
            m = self._q.get()
            if m is None:
                return
            batch = [m]
            while len(batch) < 1000:
                try:
                    nxt = self._q.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    self.send(batch)
                    return
                batch.append(nxt)
            self.send(batch)

    def send(self, batch: list[dict]) -> None:
        """파이프라인 한 번으로 XADD. 실패하면 그 묶음은 버린다(수집 DB 에는 남아 있다)."""
        pipe = self.client.pipeline(transaction=False)
        for m in batch:
            pipe.xadd(self.name, {"data": json.dumps(m, ensure_ascii=False)}, maxlen=MAXLEN, approximate=True)
        try:
            pipe.execute()
        except redis.RedisError as e:
            self._warn(f"publish failed, {len(batch)} messages dropped: {e}")

    def _warn(self, text: str) -> None:
        now = time.monotonic()
        if now - self._warned >= WARN_EVERY_SEC:
            self._warned = now
            log.warning("plant %s: %s %s", self.plant, self.name, text)

    def close(self) -> None:
        try:
            self._q.put_nowait(None)
        except queue.Full:
            self._drop_oldest()
            self._q.put_nowait(None)
        self._thread.join(timeout=TIMEOUT_SEC + 1)
        self.client.close()
