"""Nelson 알람 이벤트를 Redis Streams 로 발행한다 (Alarm Worker 가 읽어 PostgreSQL 에 적고 화면에 실시간으로 보낸다).

이벤트는 nelson_alarm 행이 열릴 때(opened)와 닫힐 때(cleared) 한 건씩이고, 필드는 event·plant 와 그 행의 컬럼 전부다
(값은 문자열, NULL 은 빈 문자열). 공장 다섯 곳이 한 스트림을 함께 쓰고 XADD MAXLEN ~ 으로 길이를 제한한다.
발행은 실시간 구간에서만 한다(PlantContext.receive). Redis 오류는 로그만 남기고 수신·판정을 멈추지 않는다 —
놓친 이벤트는 /alarms 로 메운다.
"""
from __future__ import annotations

import logging

import redis

log = logging.getLogger("collector.alarm_stream")

STREAM = "plant-sim:alarms"
MAXLEN = 10_000                 # 대략의 상한 (~). 실시간 알람은 분당 수십 건 이하라 몇 시간치가 남는다
TIMEOUT_SEC = 1.0               # Redis 가 응답하지 않을 때 생성기 워커 스레드를 붙잡는 최대 시간


class AlarmStream:
    def __init__(self, client: redis.Redis) -> None:
        self.client = client

    @classmethod
    def from_url(cls, url: str) -> "AlarmStream":
        """연결은 첫 발행 때 맺는다. 시작할 때 Redis 가 없어도 서버는 뜬다."""
        return cls(redis.Redis.from_url(url, socket_timeout=TIMEOUT_SEC, socket_connect_timeout=TIMEOUT_SEC))

    def publish(self, plant: str, events: list[dict]) -> None:
        """한 묶음의 이벤트를 파이프라인 한 번으로 보낸다."""
        pipe = self.client.pipeline(transaction=False)
        for e in events:
            fields = {"plant": plant} | {k: "" if v is None else str(v) for k, v in e.items()}
            pipe.xadd(STREAM, fields, maxlen=MAXLEN, approximate=True)
        try:
            pipe.execute()
        except redis.RedisError as e:
            log.warning("plant %s: alarm stream publish failed, %d events dropped: %s", plant, len(events), e)

    def close(self) -> None:
        self.client.close()
