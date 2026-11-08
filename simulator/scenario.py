"""시나리오 적용 (기획서 5.2~5.6).

정상 신호 위에 이벤트(이상 패턴), 정비(정지 상태), 우연한 잡음 알람을 얹는다.
모든 판단은 측정 시각의 함수라 어느 구간을 잘라 요청해도 같은 결과가 나온다.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

import numpy as np

from common.config_models import ConfigError, EquipmentConfig, PlantConfig, RuntimeConfig
from common.scenario_models import ScenarioConfig, ScenarioEvent, load_scenario
from simulator import patterns
from simulator.random_anomalies import RandomAnomalyMaker
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
        self._contrib: dict[str, list[_Contribution]] = {}        # tag_id → 값 패턴 기여
        self._sample_events: dict[str, list[ScenarioEvent]] = {}  # tag_id → freeze/quality/gap 이벤트
        self._stops: dict[str, list[tuple[float, float]]] = {}    # equip_id → 정비(정지) 구간
        self._random = RandomAnomalyMaker(plant, runtime.seed, max(c.random_anomalies.rate_per_tag_day for c in configs))
        self._index()

    # --- 준비 ---------------------------------------------------------------
    def _index(self) -> None:
        tag_owner = self.plant.tag_owner()
        by_equip = {e.equip_id: e for e in self.plant.equipment}
        driven: dict[str, list[EquipmentConfig]] = {}            # 구동기 → 피구동 설비들
        for e in self.plant.equipment:
            if e.driven_by:
                driven.setdefault(e.driven_by, []).append(e)

        for cfg in self.configs:
            cfg.validate_against(self.plant)
            for ev in cfg.events:
                if ev.pattern not in patterns.PATTERNS:
                    raise ConfigError(f"event {ev.event_id}: unknown pattern {ev.pattern}")
                targets = [ev.tag_id] if ev.tag_id else [t.tag_id for t in by_equip[ev.equip_id].tags]
                if ev.pattern in patterns.VALUE_PATTERNS:
                    for t in targets:
                        self._contrib.setdefault(t, []).append(_Contribution(ev, 1.0))
                    for r in ev.related:
                        self._contrib.setdefault(r.tag_id, []).append(_Contribution(ev, r.ratio))
                    if ev.spread_to_driven:
                        source_equip = tag_owner[ev.tag_id].equip_id if ev.tag_id else ev.equip_id
                        for d in driven.get(source_equip, []):
                            for t in d.tags:
                                if t.signal == "vibration":
                                    self._contrib.setdefault(t.tag_id, []).append(_Contribution(ev, ev.spread_to_driven))
                else:
                    for t in targets:
                        self._sample_events.setdefault(t, []).append(ev)
            for m in cfg.maintenance:
                self._stops.setdefault(m.equip_id, []).append((m.start.timestamp(), m.end.timestamp()))

    # --- 편차 (값 패턴 + 우연 알람). signal_model 의 연관 규칙이 이 함수를 원천 태그에 대해 부른다 -----
    def deviation(self, tag_id: str, epochs: np.ndarray) -> np.ndarray:
        out = np.zeros(len(epochs))
        for c in self._contrib.get(tag_id, ()):
            out += c.ratio * _event_offset(c.event, epochs)
        return out + self._random.offset(tag_id, epochs)

    # --- 적용 ---------------------------------------------------------------
    def apply(self, model: SignalModel, equip: EquipmentConfig, tag_id: str, epochs: np.ndarray):
        """(values, qualities, present). 정상 + 편차 + 연관 → 정지 → freeze → quality / gap 순서로 얹는다."""
        values = model.values(tag_id, epochs, deviation=self.deviation)
        qualities = ["Good"] * len(epochs)
        present = np.ones(len(epochs), dtype=bool)

        stopped = _mask(epochs, self._stops.get(equip.equip_id, ()))
        if stopped.any():
            values = np.where(stopped, model.stopped_values(tag_id, epochs), values)

        for ev in self._sample_events.get(tag_id, ()):
            active = _mask(epochs, [(ev.start.timestamp(), ev.end.timestamp())])
            if not active.any():
                continue
            if ev.pattern == "freeze":
                frozen = model.values(tag_id, np.array([int(ev.start.timestamp())]), deviation=self.deviation)[0]
                values = np.where(active, frozen, values)
            elif ev.pattern == "quality":
                for i in np.flatnonzero(active):
                    qualities[i] = ev.quality
            else:   # gap
                present &= ~active
        return values, qualities, present

    def model_for_tests(self) -> SignalModel:
        return SignalModel(self.plant, self.runtime.seed, self.runtime.sample_interval_sec)


def _event_offset(ev: ScenarioEvent, epochs: np.ndarray) -> np.ndarray:
    """이벤트 구간 안에서의 편차. duration 이 지나면 recovered_at 까지 마지막 값을 유지한다."""
    start, end = ev.start.timestamp(), ev.end.timestamp()
    active = (epochs >= start) & (epochs < end)
    if not active.any():
        return np.zeros(len(epochs))
    progress = np.clip((epochs - start) / ev.duration.total_seconds(), 0.0, 1.0)
    return np.where(active, patterns.shape(ev.pattern, progress, epochs, ev.magnitude), 0.0)


def _mask(epochs: np.ndarray, windows: Iterable[tuple[float, float]]) -> np.ndarray:
    mask = np.zeros(len(epochs), dtype=bool)
    for start, end in windows:
        mask |= (epochs >= start) & (epochs < end)
    return mask


def load_scenarios(paths: list[str], plant: PlantConfig, runtime: RuntimeConfig) -> Scenario:
    """여러 시나리오 파일(개발용 + 비공개 평가용)을 함께 적용한다."""
    return Scenario([load_scenario(p, plant) for p in paths], plant, runtime)
