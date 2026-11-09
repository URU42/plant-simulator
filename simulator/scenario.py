"""시나리오 적용 (기획서 5.2~5.6, 다중 공장 설계 §4.1·§4.3).

정상 신호 위에 이벤트(이상 패턴), 정비(정지 상태), 우연한 잡음 알람, 우연한 품질 저하를 얹는다.
태그는 (equip_id, tag_id) 키로 가리킨다. 모든 판단은 측정 시각의 함수라 어느 구간을 잘라 요청해도 같은 결과가 나온다.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

import numpy as np

from common.config_models import ConfigError, EquipmentConfig, PlantConfig, RuntimeConfig, TagKey
from common.scenario_models import ScenarioConfig, ScenarioEvent, load_scenario
from simulator import patterns
from simulator.random_anomalies import RandomAnomalyMaker
from simulator.random_quality import RandomQualityMaker
from simulator.signal_model import SignalModel

DAY_SEC = 86400


class _Contribution:
    """한 태그에 영향을 주는 값 패턴 이벤트와 배율."""

    __slots__ = ("event", "ratio")

    def __init__(self, event: ScenarioEvent, ratio: float) -> None:
        self.event, self.ratio = event, ratio


class Scenario:
    def __init__(self, configs: list[ScenarioConfig], plant: PlantConfig, runtime: RuntimeConfig) -> None:
        self.configs = configs
        self.plant = plant
        self.runtime = runtime
        self._contrib: dict[TagKey, list[_Contribution]] = {}        # 키 → 값 패턴 기여
        self._sample_events: dict[TagKey, list[ScenarioEvent]] = {}  # 키 → freeze/quality/gap 이벤트
        self._stops: dict[str, list[tuple[float, float]]] = {}        # equip_id → 정비(정지) 구간
        self._random = RandomAnomalyMaker(plant, runtime.seed, max(c.random_anomalies.rate_per_tag_day for c in configs))
        # 여러 파일 중 가장 센 품질 저하 설정 하나를 쓴다
        quality_cfg = max((c.random_quality for c in configs),
                          key=lambda q: (q.bad_segments_per_tag_week, q.uncertain_rate))
        self._quality = RandomQualityMaker(plant, runtime.seed, quality_cfg)
        self._index()

    # --- 준비 ---------------------------------------------------------------
    def _index(self) -> None:
        driven: dict[str, list[EquipmentConfig]] = {}            # 구동기 → 피구동 설비들
        for e in self.plant.equipment:
            if e.driven_by:
                driven.setdefault(e.driven_by, []).append(e)

        for cfg in self.configs:
            cfg.validate_against(self.plant)                     # 여기서 비어 있던 equip_id 가 채워진다
            for ev in cfg.events:
                if ev.pattern not in patterns.PATTERNS:
                    raise ConfigError(f"event {ev.event_id}: unknown pattern {ev.pattern}")
                targets = ev.target_keys(self.plant)
                if ev.pattern in patterns.VALUE_PATTERNS:
                    for key in targets:
                        self._contrib.setdefault(key, []).append(_Contribution(ev, 1.0))
                    for r in ev.related:
                        self._contrib.setdefault(r.key, []).append(_Contribution(ev, r.ratio))
                    if ev.spread_to_driven:
                        for d in driven.get(ev.equip_id, []):
                            for t in d.tags:
                                if t.signal == "vibration":
                                    self._contrib.setdefault((d.equip_id, t.tag_id), []).append(_Contribution(ev, ev.spread_to_driven))
                else:
                    for key in targets:
                        self._sample_events.setdefault(key, []).append(ev)
            for m in cfg.maintenance:
                self._stops.setdefault(m.equip_id, []).append((m.start.timestamp(), m.end.timestamp()))

    # --- 편차 (값 패턴 + 우연 알람). signal_model 의 연관 규칙이 이 함수를 원천 태그에 대해 부른다 -----
    def deviation(self, key: TagKey, epochs: np.ndarray) -> np.ndarray:
        out = np.zeros(len(epochs))
        for c in self._contrib.get(key, ()):
            out += c.ratio * _event_offset(c.event, epochs)
        return out + self._random.offset(key, epochs)

    # --- 적용 ---------------------------------------------------------------
    def apply(self, model: SignalModel, equip: EquipmentConfig, key: TagKey, epochs: np.ndarray):
        """(values, qualities, present). 정상 + 편차 + 연관 → 정지 → freeze → quality 이벤트 → 우연 품질 → gap 순서."""
        values = model.values(key, epochs, deviation=self.deviation)
        qualities = ["Good"] * len(epochs)
        present = np.ones(len(epochs), dtype=bool)

        stopped = _mask(epochs, self._stops.get(equip.equip_id, ()))
        if stopped.any():
            values = np.where(stopped, model.stopped_values(key, epochs), values)

        gap = np.zeros(len(epochs), dtype=bool)
        for ev in self._sample_events.get(key, ()):
            occurrence, active = _event_window(ev, epochs)
            if not active.any():
                continue
            if ev.pattern == "freeze":
                for occ in np.unique(occurrence[active]):      # 회차마다 그 시작 시각의 값으로 멈춘다
                    frozen = model.values(key, np.array([int(occ)]), deviation=self.deviation)[0]
                    values = np.where(active & (occurrence == occ), frozen, values)
            elif ev.pattern == "quality":
                for i in np.flatnonzero(active):
                    qualities[i] = ev.quality
            else:   # gap
                gap |= active

        random_q = self._quality.qualities(key, epochs, self.plant.tag_interval_sec(key, self.runtime.sample_interval_sec))
        if random_q is not None:                               # 이벤트가 정한 품질은 그대로 둔다
            for i in np.flatnonzero(random_q != "Good"):
                if qualities[i] == "Good":
                    qualities[i] = random_q[i]
        return values, qualities, present & ~gap

    def model_for_tests(self) -> SignalModel:
        from simulator.generator import make_signal_model
        return make_signal_model(self.plant, self.runtime.seed, self.runtime.sample_interval_sec)


def _event_window(ev: ScenarioEvent, epochs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(각 epoch 의 회차 시작, 활성 마스크). 회차 길이는 start→end (recovered_at 포함) 와 같다."""
    occurrence = ev.occurrence_start(epochs)
    length = ev.end.timestamp() - ev.start.timestamp()
    return occurrence, (epochs >= occurrence) & (epochs < occurrence + length)


def _event_offset(ev: ScenarioEvent, epochs: np.ndarray) -> np.ndarray:
    """이벤트 구간 안에서의 편차. duration 이 지나면 recovered_at 까지 마지막 값을 유지한다. repeat_every 면 회차마다."""
    occurrence, active = _event_window(ev, epochs)
    if not active.any():
        return np.zeros(len(epochs))
    progress = np.clip((epochs - occurrence) / ev.duration.total_seconds(), 0.0, 1.0)
    return np.where(active, patterns.shape(ev.pattern, progress, epochs, ev.magnitude), 0.0)


def _mask(epochs: np.ndarray, windows: Iterable[tuple[float, float]]) -> np.ndarray:
    mask = np.zeros(len(epochs), dtype=bool)
    for start, end in windows:
        mask |= (epochs >= start) & (epochs < end)
    return mask


def load_scenarios(paths: list[str], plant: PlantConfig, runtime: RuntimeConfig) -> Scenario:
    """여러 시나리오 파일(개발용 + 비공개 평가용)을 함께 적용한다."""
    return Scenario([load_scenario(p, plant) for p in paths], plant, runtime)
