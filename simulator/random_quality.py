"""우연한 품질 저하 (다중 공장 설계 §4.3). HOU 샘플 생성기 규칙의 일반화.

시나리오에 적지 않은 짧은 계측 품질 문제를 낮은 확률로 넣는다.
- Bad 구간: 태그당 주당 평균 bad_segments_per_tag_week 개(poisson), 길이는 bad_duration 사이 균등. 값이 비는 구간.
- Uncertain: 샘플마다 uncertain_rate 확률(10초 태그는 ×10). 값은 있다.
(태그, 날짜) 로 시드하므로 같은 시드면 같은 자리에 생긴다. 하루 블록을 태그당 2일 캐시한다.
"""
from __future__ import annotations

import numpy as np

from common.config_models import PlantConfig, TagConfig, TagKey
from common.scenario_models import RandomQuality
from simulator.signal_model import DAY_SEC, _seed_of

BLOCK_CACHE_DAYS = 2
_C_QUALITY = 4          # 시드 파생용 성분 번호 (random_anomalies 는 3)


class RandomQualityMaker:
    def __init__(self, plant: PlantConfig, seed: int, cfg: RandomQuality) -> None:
        self._tags: dict[TagKey, TagConfig] = plant.tag_by_key()
        self._seed = seed
        self._cfg = cfg
        self._blocks: dict[TagKey, dict[int, np.ndarray]] = {}     # 키 → {날짜: 품질 배열(object)}

    @property
    def active(self) -> bool:
        return self._cfg.uncertain_rate > 0 or self._cfg.bad_segments_per_tag_week > 0

    def qualities(self, key: TagKey, epochs: np.ndarray, interval_sec: int) -> np.ndarray | None:
        """epochs 각각의 품질(Good / Uncertain / Bad). 설정이 비어 있으면 None."""
        if not self.active or len(epochs) == 0:
            return None
        days = epochs // DAY_SEC
        idx = (epochs % DAY_SEC) // interval_sec
        out = np.empty(len(epochs), dtype=object)
        for day in np.unique(days):
            mask = days == day
            out[mask] = self._block(key, int(day), interval_sec)[idx[mask]]
        return out

    def _block(self, key: TagKey, day: int, interval_sec: int) -> np.ndarray:
        cache = self._blocks.setdefault(key, {})
        if day not in cache:
            rng = np.random.default_rng(_seed_of(self._seed, self._tags[key].seed_key, _C_QUALITY, day))
            n = DAY_SEC // interval_sec
            block = np.full(n, "Good", dtype=object)
            rate = self._cfg.uncertain_rate * interval_sec              # 10초 태그는 ×10
            if rate > 0:
                block[rng.random(n) < rate] = "Uncertain"
            lo, hi = (d.total_seconds() for d in self._cfg.bad_duration)
            for _ in range(rng.poisson(self._cfg.bad_segments_per_tag_week / 7)):
                start = int(rng.integers(0, n))
                length = max(1, int(rng.uniform(lo, hi) // interval_sec))
                block[start:start + length] = "Bad"
            cache[day] = block
            while len(cache) > BLOCK_CACHE_DAYS:
                cache.pop(next(iter(cache)))
        return cache[day]
