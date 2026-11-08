"""우연한 잡음 알람 (기획서 5.5).

시나리오에 없는 작은 이상을 낮은 확률로 무작위 삽입한다. 날짜·태그마다 seed_key 로 시드를 파생하므로
같은 시드면 같은 자리에 같은 이상이 생긴다. 크기는 경고 상한을 살짝 넘도록 잡아 대시보드에 여러 설비의
알람이 섞이게 한다.
"""
from __future__ import annotations

import numpy as np

from common.config_models import PlantConfig, TagConfig
from simulator import patterns
from simulator.signal_model import DAY_SEC, _seed_of

MIN_DURATION_SEC, MAX_DURATION_SEC = 600, 2400       # 10~40분
MAGNITUDE_RATIO = (1.05, 1.30)                        # (warn_hi − base) 에 곱하는 배율
CHOICES = ("spike", "step")


class RandomAnomalyMaker:
    def __init__(self, plant: PlantConfig, seed: int, rate_per_tag_day: float) -> None:
        self._tags: dict[str, TagConfig] = plant.tag_by_id()
        self._seed = seed
        self._rate = rate_per_tag_day
        self._cache: dict[tuple[str, int], list[tuple[int, int, str, float]]] = {}

    def offset(self, tag_id: str, epochs: np.ndarray) -> np.ndarray:
        out = np.zeros(len(epochs))
        if self._rate <= 0 or len(epochs) == 0:
            return out
        for day in range(int(epochs.min() // DAY_SEC) - 1, int(epochs.max() // DAY_SEC) + 1):
            for start, duration, pattern, magnitude in self._events(tag_id, day):
                active = (epochs >= start) & (epochs < start + duration)
                if active.any():
                    progress = np.clip((epochs - start) / duration, 0.0, 1.0)
                    out += np.where(active, patterns.shape(pattern, progress, epochs, magnitude), 0.0)
        return out

    def _events(self, tag_id: str, day: int) -> list[tuple[int, int, str, float]]:
        """(시작 epoch, 길이 초, 패턴, 크기) 목록. 하루 단위로 만들어 캐시한다."""
        key = (tag_id, day)
        if key not in self._cache:
            tag = self._tags[tag_id]
            rng = np.random.default_rng(_seed_of(self._seed, tag.seed_key, 3, day))
            events = []
            for _ in range(rng.poisson(self._rate)):
                start = day * DAY_SEC + int(rng.integers(0, DAY_SEC))
                duration = int(rng.integers(MIN_DURATION_SEC, MAX_DURATION_SEC))
                magnitude = (tag.limits.warn_hi - tag.normal.base) * rng.uniform(*MAGNITUDE_RATIO)
                events.append((start, duration, CHOICES[int(rng.integers(len(CHOICES)))], magnitude))
            self._cache[key] = events
        return self._cache[key]
