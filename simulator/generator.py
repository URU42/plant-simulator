"""구간 [start, end) 의 샘플을 만든다. 정상 신호 위에 시나리오(이상 패턴·정비)를 얹는다."""
from __future__ import annotations

from datetime import datetime

import numpy as np

from common.config_models import EquipmentConfig, PlantConfig, RuntimeConfig
from common.message import Sample
from simulator.signal_model import SignalModel, epochs_range


class SampleGenerator:
    def __init__(self, plant: PlantConfig, runtime: RuntimeConfig, scenario=None) -> None:
        self.plant = plant
        self.runtime = runtime
        self.scenario = scenario
        self.equipment: list[EquipmentConfig] = plant.equipment_for_units(runtime.units)
        self.model = SignalModel(plant, seed=runtime.seed, sample_interval_sec=runtime.sample_interval_sec)

    def generate(self, start: datetime, end: datetime) -> list[Sample]:
        """[start, end) 구간, 이 프로세스가 맡은 설비 전체의 샘플. 측정 시각 순으로 정렬한다."""
        epochs = epochs_range(start, end, self.runtime.sample_interval_sec)
        if len(epochs) == 0:
            return []
        columns: list[tuple[str, str, np.ndarray, list[str], np.ndarray]] = []
        for equip in self.equipment:
            for tag in equip.tags:
                values, qualities, present = self._tag_series(equip, tag.tag_id, epochs)
                columns.append((equip.equip_id, tag.tag_id, values, qualities, present))

        tz = start.tzinfo
        samples: list[Sample] = []
        for i, epoch in enumerate(epochs):
            ts = datetime.fromtimestamp(int(epoch), tz)
            for equip_id, tag_id, values, qualities, present in columns:
                if not present[i]:
                    continue        # gap: 데이터 끊김
                q = qualities[i]
                value = round(float(values[i]), 3) if q == "Good" else None
                samples.append(Sample(equip_id=equip_id, tag_id=tag_id, ts=ts, value=value, quality=q))
        return samples

    def _tag_series(self, equip: EquipmentConfig, tag_id: str, epochs: np.ndarray):
        """values, qualities, present 를 돌려준다. 시나리오가 없으면 정상 신호만."""
        if self.scenario is None:
            return self.model.values(tag_id, epochs), ["Good"] * len(epochs), np.ones(len(epochs), dtype=bool)
        return self.scenario.apply(self.model, equip, tag_id, epochs)
