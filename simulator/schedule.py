"""설비·KST 날짜마다 자동 이상 사건 12건을 정한다. 사건 시간 합은 하루의 9.4%(8122초, 회복 꼬리·우연 위반을 더해 합집합 ≈10%)이고, 길이는 lognormal 을
200~1800초로 잘라 합에 맞게 비례 조정하며, 나머지 시간은 Dirichlet 로 13개 간격에 나눈다. 같은 (seed, 설비, 날짜)는 항상 같은 일정이다.
"""
from __future__ import annotations

import zlib
from datetime import date, datetime

import numpy as np

from common.sim_clock import KST
from simulator.anomaly import AnomalyEvent

EVENTS_PER_DAY = 12
EVENT_FRACTION = 0.094
DAY_SEC = 86400
EVENT_TOTAL = round(DAY_SEC * EVENT_FRACTION)   # 8122


def _round_to_total(values: np.ndarray, total: int) -> list[int]:
    """정수로 반올림하고 오차는 마지막 항목에 몰아 합을 total 로 맞춘다."""
    ints = [int(round(v)) for v in values]
    ints[-1] += total - sum(ints)
    return ints


def daily_events(seed: int, equip_id: str, day: date) -> list[AnomalyEvent]:
    """KST 하루의 자동 이상 사건을 시작 순으로 돌려준다."""
    rng = np.random.default_rng([seed, zlib.crc32(equip_id.encode()), day.toordinal()])
    lengths = np.clip(rng.lognormal(np.log(650), 0.52, EVENTS_PER_DAY), 200, 1800)
    lengths = lengths * EVENT_TOTAL / lengths.sum()
    gaps = rng.dirichlet([3.0] * (EVENTS_PER_DAY + 1)) * (DAY_SEC - EVENT_TOTAL)
    lens = _round_to_total(lengths, EVENT_TOTAL)
    gps = _round_to_total(gaps, DAY_SEC - EVENT_TOTAL)
    offset = int(rng.integers(8))
    t = datetime(day.year, day.month, day.day, tzinfo=KST).timestamp()
    events = []
    for k in range(EVENTS_PER_DAY):
        t += gps[k]
        rule = (offset + k) % 8 + 1
        events.append(AnomalyEvent(
            event_id=f"SP-{equip_id}-{day:%Y%m%d}-{k + 1:02d}", origin="spontaneous", equip_id=equip_id,
            rule=rule, start=t, end=t + lens[k], normal_change=rule == 2 and bool(rng.random() < 0.2)))
        t += lens[k]
    return events
