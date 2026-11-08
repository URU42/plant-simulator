"""집계·알람·보관 배치 (기획서 7.3). 6단계에서 채운다."""
from __future__ import annotations

from collector.storage import Storage
from common.config_models import PlantConfig, RuntimeConfig


class JobRunner:
    def __init__(self, storage: Storage, plant: PlantConfig, runtime: RuntimeConfig) -> None:
        self.storage, self.plant, self.runtime = storage, plant, runtime

    def advance(self, sim_now: str | None) -> None:
        """시뮬레이터 현재 시각(sim_now)이 지난 구간을 처리한다."""
