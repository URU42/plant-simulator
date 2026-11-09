"""집계·알람·보관 배치 (기획서 7.3).

별도 스케줄러 없이, 수집 서버가 보는 시뮬레이터 현재 시각(수신한 측정 시각의 최대값)이
구간 끝을 지날 때마다 그 구간을 처리한다. 컴퓨터 시계는 보지 않으므로 백필에서도 같은 경로를 탄다.

- 10분 배치: 10분 집계 → 최대값을 상한과, 최소값을 하한과 비교해 알람(한계값 있는 태그만). 지연 판정(delay_samples)은 연속된 10분 구간 수.
          끝에 보관 기간이 지난 원시 값과 수신 묶음 기록을 지운다 (1초 데이터는 하루에 580만 행이라 매일로는 늦다).
- 매시 배치: 1시간 집계(평균, 최소, 최대, 표준편차, 상태).

한 번의 advance 는 배치마다 MAX_WINDOWS_PER_ADVANCE 구간까지만 처리한다. 수집 서버가 오래 멈췄다 켜져
시뮬레이터 시각이 크게 건너뛰어도 한 요청이 수십 초씩 멈추지 않고, 남은 구간은 다음 호출이 이어서 처리한다.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from collector.storage import Storage
from common.config_models import PlantConfig, RuntimeConfig, TagConfig, TagKey
from common.timefmt import from_db, to_db

MAX_WINDOWS_PER_ADVANCE = 144      # 한 번에 처리하는 구간 수 상한 (10분 구간 기준 하루)

TEN_MIN = timedelta(minutes=10)
ONE_HOUR = timedelta(hours=1)

# 한계 구분 → 심각도. 팀 규칙: Trip → High, Alarm → Medium, Soft → Low
SEVERITY = {"Soft": "Low", "Alarm": "Medium", "Trip": "High"}


def floor_dt(t: datetime, step: timedelta) -> datetime:
    """UTC 자정 기준으로 step 간격 경계에 내림한다."""
    day = t.replace(hour=0, minute=0, second=0, microsecond=0)
    return day + timedelta(seconds=((t - day).total_seconds() // step.total_seconds()) * step.total_seconds())


class JobRunner:
    def __init__(self, storage: Storage, plant: PlantConfig, runtime: RuntimeConfig) -> None:
        self.storage = storage
        self.runtime = runtime
        self.tags: dict[TagKey, TagConfig] = plant.tag_by_key()
        self.alarm_tags = {k: t for k, t in self.tags.items() if t.limits is not None}   # 한계값 있는 태그만 알람 판정
        self.max_delay = max((t.delay_samples for t in self.alarm_tags.values()), default=1)

    # --- 진입점 -------------------------------------------------------------
    def advance(self, sim_now: str | None, max_windows: int = MAX_WINDOWS_PER_ADVANCE) -> int:
        """시뮬레이터 현재 시각(sim_now)이 지난 구간을 처리하고, 처리한 구간 수(최대 배치별 max_windows)를 돌려준다.

        여러 번 불러도 결과는 같다. 0 을 돌려주면 더 처리할 구간이 없다는 뜻이다.
        """
        if sim_now is None:
            return 0
        now = from_db(sim_now)
        return max(self._run_windows("10m", TEN_MIN, now, self._aggregate_10m, max_windows),
                   self._run_windows("1h", ONE_HOUR, now, self._aggregate_1h, max_windows))

    def _run_windows(self, name: str, step: timedelta, now: datetime, handler, max_windows: int) -> int:
        start = self._cursor(name, step)
        if start is None:
            return 0
        done = 0
        while start + step <= now and done < max_windows:
            with self.storage.transaction():
                handler(start, start + step)
                self.storage.execute("INSERT OR REPLACE INTO job_state (name, last_bucket_end) VALUES (?, ?)",
                                     (name, to_db(start + step)))
            start += step
            done += 1
        return done

    def _cursor(self, name: str, step: timedelta) -> datetime | None:
        """다음에 처리할 구간의 시작. 처음이면 가장 오래된 원시 값의 구간부터."""
        row = self.storage.query("SELECT last_bucket_end FROM job_state WHERE name = ?", (name,))
        if row:
            return from_db(row[0][0])
        first = self.storage.query("SELECT MIN(ts) FROM ts_raw")[0][0]
        return floor_dt(from_db(first), step) if first else None

    # --- 집계 ---------------------------------------------------------------
    def _aggregate(self, table: str, start: datetime, end: datetime) -> dict[TagKey, dict]:
        """[start, end) 구간을 태그별로 집계해 table 에 넣는다. 값이 있는 행(Good·Uncertain)으로 평균·최소·최대를 구하고,
        값이 없는 태그는 NoData 로 남긴다."""
        found = {(r["equip_id"], r["tag_id"]): r for r in self.storage.query_dicts(
            "SELECT equip_id, tag_id, AVG(value) AS avg, MIN(value) AS min, MAX(value) AS max, COUNT(value) AS count, "
            "AVG(value * value) AS sq, SUM(quality NOT IN ('Good', 'Uncertain')) AS bad "
            "FROM ts_raw WHERE ts >= ? AND ts < ? GROUP BY equip_id, tag_id", (to_db(start), to_db(end)))}
        rows = []
        for (equip_id, tag_id) in self.tags:
            r = found.get((equip_id, tag_id))
            if r and r["count"]:
                sd = math.sqrt(max(r["sq"] - r["avg"] ** 2, 0.0))
                rows.append((equip_id, tag_id, to_db(start), r["avg"], r["min"], r["max"], sd, r["count"], "Good"))
            else:
                rows.append((equip_id, tag_id, to_db(start), None, None, None, None, 0, "Bad" if r else "NoData"))
        self.storage.executemany(
            f"INSERT OR REPLACE INTO {table} (equip_id, tag_id, bucket_start, avg, min, max, sd, count, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        return found

    def _aggregate_10m(self, start: datetime, end: datetime) -> None:
        self._aggregate("ts_10m", start, end)
        self._evaluate_alarms(start)
        self._retention(end)

    def _aggregate_1h(self, start: datetime, end: datetime) -> None:
        self._aggregate("ts_1h", start, end)

    # --- 알람 ---------------------------------------------------------------
    def _evaluate_alarms(self, bucket_start: datetime) -> None:
        """최근 delay_samples 개 10분 구간이 연속으로 한계를 벗어나면 알람을 만들고, 돌아오면 해제한다.

        상한(hi)은 구간 최대값이 한계를 넘을 때, 하한(lo)은 구간 최소값이 한계 아래일 때. 열린 알람은 (키, 한계 구분, 방향)마다 하나.
        """
        since = bucket_start - TEN_MIN * (self.max_delay - 1)
        history: dict[TagKey, list[tuple[float | None, float | None]]] = {}     # 키 → [(max, min)] 오래된 것부터
        for equip_id, tag_id, mx, mn in self.storage.query(
                "SELECT equip_id, tag_id, max, min FROM ts_10m WHERE bucket_start >= ? AND bucket_start <= ? "
                "ORDER BY equip_id, tag_id, bucket_start", (to_db(since), to_db(bucket_start))):
            history.setdefault((equip_id, tag_id), []).append((mx, mn))
        open_alarms = {(r["equip_id"], r["tag_id"], r["limit_type"], r["direction"]): r["alarm_id"] for r in self.storage.query_dicts(
            "SELECT alarm_id, equip_id, tag_id, limit_type, direction FROM alarm_detail WHERE cleared_at IS NULL")}

        when = to_db(bucket_start)
        for key, tag in self.alarm_tags.items():
            windows = history.get(key, [])
            checks = [("hi", [w[0] for w in windows], tag.limits.hi_levels(), lambda v, lim: v > lim),
                      ("lo", [w[1] for w in windows], tag.limits.lo_levels(), lambda v, lim: v < lim)]
            for direction, series, levels, beyond in checks:
                current = series[-1] if series else None
                recent = series[-tag.delay_samples:]
                for limit_type, limit in levels:
                    exceeded = len(recent) == tag.delay_samples and all(v is not None and beyond(v, limit) for v in recent)
                    alarm_key = (*key, limit_type, direction)
                    if exceeded and alarm_key not in open_alarms:
                        self.storage.execute(
                            "INSERT INTO alarm_detail (occurred_at, equip_id, tag_id, limit_type, direction, limit_value, "
                            "actual_value, exceed_pct, severity) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (when, key[0], key[1], limit_type, direction, limit, current,
                             _exceed_pct(current, limit, direction), SEVERITY[limit_type]))
                    elif alarm_key in open_alarms and (current is None or not beyond(current, limit)):
                        self.storage.execute("UPDATE alarm_detail SET cleared_at = ? WHERE alarm_id = ?",
                                             (when, open_alarms[alarm_key]))

    # --- 보관 ---------------------------------------------------------------
    def _retention(self, now: datetime) -> None:
        """보관 기간이 지난 원시 값(측정 시각 기준)과 수신 묶음 기록(받은 벽시계 시각 기준)을 지운다. 집계는 남긴다.

        그보다 오래된 묶음은 측정 시각도 보관 기간 밖이라 어차피 거절되므로, batch_id 중복 확인 창이 줄어도 결과는 같다.
        """
        keep = timedelta(days=self.runtime.raw_retention_days)
        self.storage.execute("DELETE FROM ts_raw WHERE ts < ?", (to_db(now - keep),))
        self.storage.execute("DELETE FROM ingest_batch WHERE received_at < ?", (to_db(datetime.now(timezone.utc) - keep),))


def _exceed_pct(actual: float, limit: float, direction: str) -> float:
    """한계 대비 벗어난 비율(%). 하한은 (한계 − 값) / |한계|. 한계가 0 이면 0."""
    if limit == 0:
        return 0.0
    return ((actual - limit) if direction == "hi" else (limit - actual)) / abs(limit) * 100
