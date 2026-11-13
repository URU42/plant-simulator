"""배포 설정 (k8s 설계 §4.2). 환경 변수로 받고, 명령행 인자가 있으면 덮어쓴다.

ROOT_PATH          Ingress 경로 접두어 (예: /plant-sim). 루트 목록 링크·리다이렉트·/docs 가 이 접두어를 붙인다
CORS_ORIGINS       쉼표 구분 origin 목록. 있으면 GET 에 CORS 헤더를 붙인다
INGEST_TOKEN       있으면 POST /{공장}/ingest·/{공장}/anomalies 는 헤더 X-Ingest-Token 이 같아야 한다. 같은 프로세스의 생성기는 거치지 않는다
WITH_SIMULATORS    1/true 면 공장 목록의 생성기를 같은 프로세스에서 돌린다
PERSISTENT_FAULT_INTERVAL_MIN  있으면 모든 공장 runtime 의 지속 고장 간격(분)을 덮어쓴다. 0 이면 끈다
PERSISTENT_FAULT_MAX_OPEN      있으면 모든 공장 runtime 의 동시 지속 고장 상한(1 이상의 정수)을 덮어쓴다
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

from common.config_models import ConfigError

TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass
class Settings:
    root_path: str = ""
    cors_origins: list[str] = field(default_factory=list)
    ingest_token: str | None = None
    with_simulators: bool = False
    time_mode: str | None = None          # 통합 실행에서 각 공장 runtime 의 time_mode 를 덮어쓴다
    backfill_days: int | None = None      # 통합 실행에서 각 공장 runtime 의 backfill_days 를 덮어쓴다
    persistent_fault_interval_min: float | None = None   # 각 공장 runtime 의 지속 고장 간격(분)을 덮어쓴다
    persistent_fault_max_open: int | None = None         # 각 공장 runtime 의 동시 지속 고장 상한을 덮어쓴다

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        """환경 변수에서 읽고, None 이 아닌 overrides 로 덮어쓴다."""
        env = os.environ
        s = cls(
            root_path=env.get("ROOT_PATH", "").rstrip("/"),
            cors_origins=[o.strip() for o in env.get("CORS_ORIGINS", "").split(",") if o.strip()],
            ingest_token=env.get("INGEST_TOKEN") or None,
            with_simulators=env.get("WITH_SIMULATORS", "").strip().lower() in TRUE_VALUES,
            persistent_fault_interval_min=_interval_min(env.get("PERSISTENT_FAULT_INTERVAL_MIN")),
            persistent_fault_max_open=_max_open(env.get("PERSISTENT_FAULT_MAX_OPEN")),
        )
        for name, value in overrides.items():
            if value is not None:
                setattr(s, name, value)
        return s


def _interval_min(raw: str | None) -> float | None:
    """PERSISTENT_FAULT_INTERVAL_MIN 값(분). 비었으면 None. 0 이상의 수가 아니면 변수 이름을 담은 ConfigError."""
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0:
        raise ConfigError(f"PERSISTENT_FAULT_INTERVAL_MIN must be a number of minutes ≥ 0 (0 turns it off), got {raw!r}")
    return value


def _max_open(raw: str | None) -> int | None:
    """PERSISTENT_FAULT_MAX_OPEN 값. 비었으면 None. 1 이상의 정수가 아니면 변수 이름을 담은 ConfigError."""
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise ConfigError(f"PERSISTENT_FAULT_MAX_OPEN must be an integer ≥ 1, got {raw!r}")
    return value
