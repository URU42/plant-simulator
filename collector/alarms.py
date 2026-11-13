"""수집 서버 상시 Nelson 판정 (설계 2026-11-12-nelson-anomaly §4.8). 받은 묶음마다 태그별로 시각 순 정렬해 common.nelson.judge 로
8규칙 비트를 구하고, (태그, 규칙)마다 연속 위반을 nelson_alarm 한 행으로 묶는다. 묶음은 그 규칙이 처음 성립한 표본에서 열고,
15 표본 연속으로 성립하지 않으면 그 15 표본 중 첫 표본 시각으로 닫는다. 태그별 최근 z 꼬리(최대 14개)와 마지막 판정 시각,
열린 묶음은 메모리에 두고 시작할 때 ts_raw·tag_current·nelson_alarm 에서 채운다. 마지막 판정 시각 이하인 행(늦게 온 행)은
판정하지 않는다. 받은 status 는 판정 입력이 아니며, 판정과 다른 표본 수만 돌려준다. 쓰기는 호출자(PlantContext)의 락 안에서 한다.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from collector.storage import Storage
from common.config_models import PlantConfig, TagKey
from common.nelson import HISTORY, bit, judge, zscore

CLOSE_AFTER = 15                    # 이만큼 연속으로 규칙이 성립하지 않으면 묶음을 닫는다
RULES = range(1, 9)
DIRECTED = {1, 2, 3, 5, 6}          # 방향(hi/lo)이 있는 규칙. 4·7·8 은 direction NULL


@dataclass
class _Episode:
    """(태그, 규칙)의 열린 묶음. alarm_id 가 None 이면 이번 묶음에서 열려 아직 INSERT 하지 않은 것."""

    alarm_id: int | None
    rule: int
    direction: str | None
    occurred_at: str
    value: float                    # value_at_occurrence
    hit_count: int
    last_hit_at: str
    misses: int = 0                 # 마지막 성립 뒤 연속으로 성립하지 않은 표본 수
    miss_start: str | None = None   # 그 연속의 첫 표본 시각 (닫힐 때 cleared_at)


class AlarmTracker:
    def __init__(self, storage: Storage, plant: PlantConfig) -> None:
        self.storage = storage
        self.control = {key: (t.control.mean, t.control.sigma) for key, t in plant.tag_by_key().items()}
        # 태그별 최근 z (오래된 것부터, 최대 HISTORY 개)
        self.tail: dict[TagKey, list[float]] = {
            key: [zscore(v, *self.control[key]) for v in values]
            for key, values in storage.recent_values(HISTORY).items() if key in self.control}
        # 태그별 마지막 판정 시각 = 받은 측정 시각의 최대 (tag_current 는 더 새로운 시각만 반영하므로 ts_raw 의 MAX(ts) 와 같다)
        self.last_ts: dict[TagKey, str] = {(r[0], r[1]): r[2] for r in storage.query("SELECT equip_id, tag_id, ts FROM tag_current")}
        # (태그, 규칙)별 열린 묶음. 재시작하면 연속 미성립 수는 0 부터 다시 센다
        self.open: dict[tuple[TagKey, int], _Episode] = {
            ((r["equip_id"], r["tag_id"]), r["rule"]): _Episode(
                r["alarm_id"], r["rule"], r["direction"], r["occurred_at"], r["value_at_occurrence"], r["hit_count"], r["last_hit_at"])
            for r in storage.query_dicts("SELECT * FROM nelson_alarm WHERE cleared_at IS NULL")}

    def observe(self, rows: list[tuple[str, str, str, float, str]]) -> int:
        """rows: (equip_id, tag_id, ts_db, value, status). 판정해 nelson_alarm 에 반영하고, 받은 status 와 판정
        (비트가 있으면 Bad)이 다른 표본 수를 돌려준다."""
        by_tag: dict[TagKey, dict[str, tuple[float, str]]] = defaultdict(dict)
        for equip_id, tag_id, ts, value, status in rows:
            key = (equip_id, tag_id)
            if key in self.control and value is not None and (key not in self.last_ts or ts > self.last_ts[key]):
                by_tag[key].setdefault(ts, (value, status))      # 같은 시각은 처음 것 (ts_raw 의 INSERT OR IGNORE 와 같다)

        mismatches = 0
        touched: dict[int, _Episode] = {}       # 이번 묶음에서 바뀐 기존 행 (alarm_id → 묶음)
        closed: list[tuple[TagKey, _Episode]] = []   # 이번 묶음에서 열리고 닫힌 새 행
        for key, samples in by_tag.items():
            ts = sorted(samples)
            values = np.array([samples[t][0] for t in ts])
            z = zscore(values, *self.control[key])
            hits, low = judge(z, np.array(self.tail.get(key, ())))
            mismatches += int(np.count_nonzero((hits != 0) != np.array([samples[t][1] == "Bad" for t in ts])))
            self.tail[key] = (self.tail.get(key, []) + z.tolist())[-HISTORY:]
            self.last_ts[key] = ts[-1]
            for rule in RULES:
                b = bit(rule)
                for ep, is_closed in self._track(key, rule, ts, values, np.flatnonzero(hits & b), low & b):
                    if ep.alarm_id is not None:
                        touched[ep.alarm_id] = ep
                    elif is_closed:
                        closed.append((key, ep))
        self._write(touched, closed)
        return mismatches

    def _track(self, key: TagKey, rule: int, ts: list[str], values: np.ndarray, hit_idx: np.ndarray,
               low: np.ndarray) -> list[tuple[_Episode, bool]]:
        """한 (태그, 규칙)의 묶음을 이번 표본들로 진행시킨다. DB 에 반영할 묶음과 닫혔는지를 돌려준다.
        성립한 표본만 차례로 보며, 그 사이 간격으로 연속 미성립 수를 센다."""
        ep = self.open.get((key, rule))
        if ep is None and len(hit_idx) == 0:
            return []
        changed: list[tuple[_Episode, bool]] = []
        prev = -1                                   # 이전 성립 표본 위치 (이번 묶음 안)
        for i in [*hit_idx.tolist(), len(ts)]:      # 끝 표시(len(ts))로 마지막 성립 뒤 남은 표본까지 센다
            if ep is not None:
                gap = i - prev - 1                  # prev 다음부터 i 전까지 성립하지 않은 표본
                if gap and ep.misses == 0:
                    ep.miss_start = ts[prev + 1]
                ep.misses += gap
                if ep.misses >= CLOSE_AFTER:
                    changed.append((ep, True))
                    ep = None
                elif i < len(ts):
                    ep.hit_count += 1
                    ep.last_hit_at = ts[i]
                    ep.misses, ep.miss_start = 0, None
            if ep is None and i < len(ts):
                direction = ("lo" if low[i] else "hi") if rule in DIRECTED else None
                ep = _Episode(None, rule, direction, ts[i], float(values[i]), 1, ts[i])
            prev = i
        if ep is None:
            self.open.pop((key, rule), None)
        else:
            self.open[(key, rule)] = ep
            if len(hit_idx):                        # 성립이 없었으면 DB 는 그대로다 (미성립 수는 메모리에만)
                changed.append((ep, False))
        return changed

    def _write(self, touched: dict[int, _Episode], closed: list[tuple[TagKey, _Episode]]) -> None:
        """기존 행은 executemany 로 고치고, 새 행은 넣는다. 아직 열린 새 행은 alarm_id 를 받아 둔다."""
        fresh = [(key, ep) for (key, _), ep in self.open.items() if ep.alarm_id is None]
        if not touched and not closed and not fresh:
            return
        insert = ("INSERT INTO nelson_alarm (equip_id, tag_id, rule, direction, occurred_at, cleared_at, last_hit_at, "
                  "hit_count, value_at_occurrence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)")

        def row(key: TagKey, ep: _Episode, cleared: str | None) -> tuple:
            return (*key, ep.rule, ep.direction, ep.occurred_at, cleared, ep.last_hit_at, ep.hit_count, ep.value)

        with self.storage.transaction():
            self.storage.executemany(
                "UPDATE nelson_alarm SET hit_count = ?, last_hit_at = ?, cleared_at = ? WHERE alarm_id = ?",
                [(ep.hit_count, ep.last_hit_at, ep.miss_start if ep.misses >= CLOSE_AFTER else None, alarm_id)
                 for alarm_id, ep in touched.items()])
            self.storage.executemany(insert, [row(key, ep, ep.miss_start) for key, ep in closed])
            for key, ep in fresh:
                ep.alarm_id = self.storage.execute(insert, row(key, ep, None)).lastrowid
