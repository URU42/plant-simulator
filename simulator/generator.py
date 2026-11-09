"""구간 [start, end) 의 샘플을 만든다. 정상 신호 위에 시나리오(이상 패턴·정비)를 얹는다.

태그마다 측정 주기(interval_sec)가 다를 수 있다. 1초 구간을 요청해도 10초 태그는 10초 격자에 걸릴 때만 샘플이 생긴다.
"""
from __future__ import annotations

from datetime import datetime

import numpy as np

from common.config_models import EquipmentConfig, PlantConfig, RuntimeConfig, TagConfig
from common.message import Sample
from simulator.signal_model import SignalModel, epochs_range


def make_signal_model(plant: PlantConfig, seed: int, sample_interval_sec: int) -> SignalModel:
    """구성안의 plant.signal_model 로 신호 모델을 고른다."""
    if plant.plant.signal_model == "rfcc":
        from simulator.rfcc_model import RfccSignalModel
        return RfccSignalModel(plant, seed, sample_interval_sec)
    return SignalModel(plant, seed, sample_interval_sec)


class SampleGenerator:
    def __init__(self, plant: PlantConfig, runtime: RuntimeConfig, scenario=None) -> None:
        runtime.validate_tag_intervals(plant)
        self.plant = plant
        self.runtime = runtime
        self.scenario = scenario
        self.equipment: list[EquipmentConfig] = plant.equipment_for_units(runtime.units)
        self.model = make_signal_model(plant, runtime.seed, runtime.sample_interval_sec)

    def generate(self, start: datetime, end: datetime) -> list[Sample]:
        """[start, end) 구간, 이 프로세스가 맡은 설비 전체의 샘플. 측정 시각 순으로 정렬한다."""
        epochs = epochs_range(start, end, self.runtime.sample_interval_sec)
        if len(epochs) == 0:
            return []
        # 태그별 열: (설비 ID, 태그, 이 태그가 걸리는 epoch 의 위치, 값, 품질, 존재 여부)
        columns: list[tuple[str, TagConfig, np.ndarray, np.ndarray, list[str], np.ndarray]] = []
        for equip in self.equipment:
            for tag in equip.tags:
                positions = np.flatnonzero(epochs % self.plant.tag_interval_sec(tag.tag_id, self.runtime.sample_interval_sec) == 0)
                if len(positions) == 0:
                    continue
                values, qualities, present = self._tag_series(equip, tag.tag_id, epochs[positions])
                columns.append((equip.equip_id, tag, positions, values, qualities, present))

        tz = start.tzinfo
        rows: list[tuple[int, int, Sample]] = []     # (epoch 위치, 열 번호, 샘플) → 측정 시각 순 정렬용
        for col, (equip_id, tag, positions, values, qualities, present) in enumerate(columns):
            for j, i in enumerate(positions):
                if not present[j]:
                    continue        # gap: 데이터 끊김
                q = qualities[j]
                value = round(float(values[j]), tag.decimals) if q == "Good" else None
                ts = datetime.fromtimestamp(int(epochs[i]), tz)
                # 값은 이미 검증된 설정에서 나오므로 model_construct 로 빠르게 만든다
                rows.append((i, col, Sample.model_construct(equip_id=equip_id, tag_id=tag.tag_id, ts=ts, value=value, quality=q)))
        rows.sort(key=lambda r: (r[0], r[1]))
        return [s for _, _, s in rows]

    def _tag_series(self, equip: EquipmentConfig, tag_id: str, epochs: np.ndarray):
        """values, qualities, present 를 돌려준다. 시나리오가 없으면 정상 신호만."""
        if self.scenario is None:
            return self.model.values(tag_id, epochs), ["Good"] * len(epochs), np.ones(len(epochs), dtype=bool)
        return self.scenario.apply(self.model, equip, tag_id, epochs)
