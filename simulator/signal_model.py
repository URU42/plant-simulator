"""정상 신호 모델과 연관 규칙 (기획서 5.1, 5.2, 5.7).

모든 값은 (seed, seed_key, 측정 시각)의 순수 함수다. 그래서
- 백필 → 실시간으로 넘어가도 이어지고,
- Unit별로 프로세스를 나눠 돌려도 같은 값이 나오고,
- 같은 시드로 다시 돌리면 같은 과거 데이터가 나온다.

정상값 = 기준값 + 하루 주기 변동 + 작은 잡음 + 천천히 떠도는 성분(상·하한이 있는 랜덤 워크)
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Callable

import numpy as np

from common.config_models import PlantConfig, TagConfig
from common.sim_clock import KST

# 편차 제공자: (tag_id, epoch 초 배열) → 그 태그 자신의 이상 편차 배열. 시나리오 쪽에서 넘겨준다.
DeviationFn = Callable[[str, np.ndarray], np.ndarray]

DRIFT_EPOCH = datetime(2020, 1, 1, tzinfo=KST).timestamp()   # 랜덤 워크의 고정 기준 시각
DRIFT_BOUND_STEPS = 10          # 랜덤 워크 상·하한 = drift_sd × 이 값
DAY_SEC = 86400
HOUR_SEC = 3600
AMBIENT_TEMP_C = 25.0           # 정지 상태에서 온도가 내려가는 주변 온도
STOPPED_NOISE_RATIO = 0.3       # 정지 상태 잡음 비율


def epochs_range(start: datetime, end: datetime, interval_sec: int) -> np.ndarray:
    """[start, end) 구간의 측정 시각을 epoch 초(int64) 배열로 만든다."""
    return np.arange(int(start.timestamp()), int(end.timestamp()), interval_sec, dtype=np.int64)


def _seed_of(seed: int, seed_key: str, *parts: int) -> list[int]:
    """태그별 시드. tag_id가 아니라 seed_key에서 파생한다(3.4)."""
    digest = hashlib.sha256(seed_key.encode()).digest()
    return [seed, int.from_bytes(digest[:4], "big"), *parts]


class TagSignal:
    """한 태그의 정상 신호 생성기."""

    def __init__(self, tag: TagConfig, seed: int, sample_interval_sec: int) -> None:
        self.tag = tag
        self._seed = seed
        self._interval = sample_interval_sec
        self._noise_cache: dict[int, np.ndarray] = {}
        self._drift_knots: np.ndarray = np.zeros(0)
        # 하루 주기의 위상은 태그마다 다르게 둔다
        self._daily_phase = np.random.default_rng(_seed_of(seed, tag.seed_key, 0)).uniform(0, 2 * np.pi)

    def normal(self, epochs: np.ndarray) -> np.ndarray:
        n = self.tag.normal
        return n.base + self._daily(epochs) + self._noise(epochs) + self._drift(epochs)

    def stopped(self, epochs: np.ndarray) -> np.ndarray:
        """정지 상태: 진동·압력은 0 근처, 온도는 주변 온도 쪽."""
        rest = {"temperature": AMBIENT_TEMP_C}.get(self.tag.signal, 0.0)
        return rest + self._noise(epochs) * STOPPED_NOISE_RATIO

    # --- 성분 ---------------------------------------------------------------
    def _daily(self, epochs: np.ndarray) -> np.ndarray:
        local_sec = (epochs + int(KST.utcoffset(None).total_seconds())) % DAY_SEC
        return self.tag.normal.daily_amp * np.sin(2 * np.pi * local_sec / DAY_SEC + self._daily_phase)

    def _noise(self, epochs: np.ndarray) -> np.ndarray:
        """잡음은 날짜 블록 단위로 시드를 파생해 만든다. 어느 구간을 잘라 요청해도 같은 값."""
        days = epochs // DAY_SEC
        idx = (epochs % DAY_SEC) // self._interval
        out = np.empty(len(epochs))
        for day in np.unique(days):
            block = self._noise_block(int(day))
            mask = days == day
            out[mask] = block[idx[mask]]
        return out

    def _noise_block(self, day: int) -> np.ndarray:
        if day not in self._noise_cache:
            rng = np.random.default_rng(_seed_of(self._seed, self.tag.seed_key, 1, day))
            self._noise_cache[day] = rng.normal(0.0, self.tag.normal.noise_sd, DAY_SEC // self._interval)
            if len(self._noise_cache) > 8:
                self._noise_cache.pop(next(iter(self._noise_cache)))
        return self._noise_cache[day]

    def _drift(self, epochs: np.ndarray) -> np.ndarray:
        """기준 시각부터 시간별 랜덤 워크를 만들고(상·하한은 반사), 시간 사이는 선형 보간한다."""
        hours = (epochs - DRIFT_EPOCH) / HOUR_SEC
        needed = int(hours.max()) + 2
        if len(self._drift_knots) < needed:
            self._drift_knots = self._make_drift_knots(needed)
        return np.interp(hours, np.arange(len(self._drift_knots)), self._drift_knots)

    def _make_drift_knots(self, count: int) -> np.ndarray:
        sd = self.tag.normal.drift_sd
        if sd <= 0:
            return np.zeros(count)
        rng = np.random.default_rng(_seed_of(self._seed, self.tag.seed_key, 2))
        walk = np.cumsum(rng.normal(0.0, sd, count))
        bound = sd * DRIFT_BOUND_STEPS
        # 구간 [-bound, bound] 밖으로 나가면 반사시켜 상·하한을 지킨다 (벡터 연산)
        return bound - np.abs((walk + bound) % (4 * bound) - 2 * bound)


class SignalModel:
    """구성안 전체의 정상 신호 + 연관 규칙."""

    def __init__(self, plant: PlantConfig, seed: int, sample_interval_sec: int) -> None:
        self.plant = plant
        self._signals = {t.tag_id: TagSignal(t, seed, sample_interval_sec) for t in plant.all_tags()}
        self._incoming: dict[str, list] = {}       # target tag_id → [Correlation]
        for c in plant.correlations:
            self._incoming.setdefault(c.target, []).append(c)

    def normal_values(self, tag_id: str, epochs: np.ndarray) -> np.ndarray:
        return self._signals[tag_id].normal(epochs)

    def stopped_values(self, tag_id: str, epochs: np.ndarray) -> np.ndarray:
        return self._signals[tag_id].stopped(epochs)

    def values(self, tag_id: str, epochs: np.ndarray, deviation: DeviationFn | None = None) -> np.ndarray:
        """정상값 + 자신의 이상 편차 + 연관 규칙(원천 태그 편차 × gain, lag 만큼 늦게)."""
        out = self.normal_values(tag_id, epochs)
        if deviation is None:
            return out
        out = out + deviation(tag_id, epochs)
        for c in self._incoming.get(tag_id, ()):
            out = out + c.gain * deviation(c.source, epochs - c.lag_minutes * 60)
        return out
