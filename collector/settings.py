"""배포 설정 (k8s 설계 §4.2). 환경 변수로 받고, 명령행 인자가 있으면 덮어쓴다.

ROOT_PATH          Ingress 경로 접두어 (예: /plant-sim). 루트 목록 링크·리다이렉트·/docs 가 이 접두어를 붙인다
CORS_ORIGINS       쉼표 구분 origin 목록. 있으면 GET 에 CORS 헤더를 붙인다
INGEST_TOKEN       있으면 POST /{공장}/ingest·/{공장}/anomalies 는 헤더 X-Ingest-Token 이 같아야 한다. 같은 프로세스의 생성기는 거치지 않는다
WITH_SIMULATORS    1/true 면 공장 목록의 생성기를 같은 프로세스에서 돌린다
REDIS_URL          있으면 받은 값을 공장별 Redis Stream plant-sim:data:{공장} 으로 보낸다 (collector.data_stream)
RESET_DB_ON_SCHEMA_CHANGE  1/true 면 스키마 버전이 다른 공장 DB 파일을 지우고 새로 만든다(백필로 다시 채움). 기본은 오류로 멈춘다
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass
class Settings:
    root_path: str = ""
    cors_origins: list[str] = field(default_factory=list)
    ingest_token: str | None = None
    with_simulators: bool = False
    redis_url: str | None = None
    reset_db_on_schema_change: bool = False
    time_mode: str | None = None          # 통합 실행에서 각 공장 runtime 의 time_mode 를 덮어쓴다
    backfill_days: int | None = None      # 통합 실행에서 각 공장 runtime 의 backfill_days 를 덮어쓴다

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        """환경 변수에서 읽고, None 이 아닌 overrides 로 덮어쓴다."""
        env = os.environ
        s = cls(
            root_path=env.get("ROOT_PATH", "").rstrip("/"),
            cors_origins=[o.strip() for o in env.get("CORS_ORIGINS", "").split(",") if o.strip()],
            ingest_token=env.get("INGEST_TOKEN") or None,
            with_simulators=env.get("WITH_SIMULATORS", "").strip().lower() in TRUE_VALUES,
            redis_url=env.get("REDIS_URL") or None,
            reset_db_on_schema_change=env.get("RESET_DB_ON_SCHEMA_CHANGE", "").strip().lower() in TRUE_VALUES,
        )
        for name, value in overrides.items():
            if value is not None:
                setattr(s, name, value)
        return s

