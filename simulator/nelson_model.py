"""Nelson 신호 생성기. 태그마다 관리 기준(μ·σ) 대비 z 를 측정 시각 순으로 만든다. 평소에는 AR(1) 표집에서
규칙 위반이 생기는 후보를 다시 뽑아(최대 100번) 조용한 신호를 내고, 사건(자동 이상·주입·지속 고장)의 반응 구간에서는
사건 모양의 z 를 그대로 쓴다. 값은 round(μ + σ·z) 이고 판정은 반올림한 값의 z 로 하며, 위반 비트가 있으면 Bad 다.
태그마다 최근 14개 z 와 그날의 난수열을 상태로 들고 창에서 창으로 이어 가며, restore 로 수집 서버의 마지막 값에서 다시 시작한다.
"""
from __future__ import annotations

import math
import zlib
from datetime import date, datetime
from functools import lru_cache
from typing import Protocol

import numpy as np

from common.config_models import PlantConfig, RuntimeConfig, TagKey
from common.message import Sample
from common.nelson import NelsonState, zscore
from simulator.anomaly import MAX_RECOVERY_SEC, AnomalyEvent, Response, resolve, responses, tag_z
from simulator.schedule import daily_events

RHO = 0.12                  # AR(1) 계수
RHO_TEMP = 0.25             # 온도 신호의 AR(1) 계수
CLIP = 2.0                  # AR 에 넣는 직전 z 의 상·하한
TRIES = 100                 # 억제 표집의 최대 시도 수
BATCH = 256                 # 난수를 한 번에 뽑는 개수
DAY_SEC = 86400
KST_OFFSET = 9 * 3600
EPOCH_ORDINAL = date(1970, 1, 1).toordinal()
EXTERNAL_LOOKBACK_SEC = 3600 + MAX_RECOVERY_SEC   # 외부 사건을 되돌아보는 길이 (가장 긴 자동 이상 < 1시간 + 회복)


class EventSource(Protocol):
    """생성기 밖에서 오는 사건(주입·지속 고장)과 멈춤, 자동 이상이 열렸다는 알림을 받는 쪽.
    external 은 [start, end) 와 겹치는 외부 사건을 모두 돌려준다(end=None 은 열린 사건, 멈춤은 stops 로 따로 알린다).
    opened 는 재시작·restore 뒤 같은 event_id 로 다시 불릴 수 있으므로 event_id 기준으로 멱등이어야 한다.
    """

    def external(self, start: float, end: float) -> list[AnomalyEvent]: ...
    def stops(self) -> dict[str, float]: ...
    def opened(self, event: AnomalyEvent) -> None: ...


class NoEvents:
    """아무 사건도 없는 EventSource."""

    def external(self, start: float, end: float) -> list[AnomalyEvent]:
        return []

    def stops(self) -> dict[str, float]:
        return {}

    def opened(self, event: AnomalyEvent) -> None:
        pass


def _kst_day(epoch: float) -> int:
    """epoch 초의 KST 날짜 ordinal."""
    return int(epoch + KST_OFFSET) // DAY_SEC + EPOCH_ORDINAL


@lru_cache(maxsize=1024)
def _daily(seed: int, equip_id: str, day: int) -> tuple[AnomalyEvent, ...]:
    return tuple(daily_events(seed, equip_id, date.fromordinal(day)))


class _Tag:
    """한 태그의 고정 값(기준·주기·AR 계수)과 생성 상태(판정 상태, AR 직전 z, 그날의 난수)."""

    __slots__ = ("key", "mu", "sigma", "decimals", "interval", "rho", "scale", "crc", "driver",
                 "nelson", "zp", "rng", "day", "normals", "uniforms")

    def __init__(self, key: TagKey, mu: float, sigma: float, decimals: int, interval: int, rho: float,
                 seed_key: str, driver: TagKey | None) -> None:
        self.key = key
        self.mu, self.sigma, self.decimals, self.interval = mu, sigma, decimals, interval
        self.rho, self.scale = rho, math.sqrt(1 - rho * rho)
        self.crc = zlib.crc32(seed_key.encode())
        self.driver = driver                # 피구동 speed 태그면 구동기 speed 태그의 키
        self.nelson = NelsonState()
        self.zp = 0.0                       # 직전에 보낸(반올림한) 값의 z
        self.rng: np.random.Generator | None = None
        self.day: int | None = None         # rng 를 시드한 KST 날짜
        self.normals: list[float] = []
        self.uniforms: list[float] = []

    def reseed(self, seed: list[int], day: int) -> None:
        self.rng = np.random.default_rng(seed)
        self.day = day
        self.normals, self.uniforms = [], []

    def normal(self) -> float:
        if not self.normals:
            self.normals = self.rng.standard_normal(BATCH).tolist()
        return self.normals.pop()

    def uniform(self) -> float:
        if not self.uniforms:
            self.uniforms = self.rng.uniform(-1.0, 1.0, BATCH).tolist()
        return self.uniforms.pop()


class NelsonGenerator:
    def __init__(self, plant: PlantConfig, runtime: RuntimeConfig, source: EventSource | None = None,
                 spontaneous: bool = True) -> None:
        runtime.validate_tag_intervals(plant)
        self.plant = plant
        self.runtime = runtime
        self.source = source or NoEvents()
        self.spontaneous = spontaneous
        self.equipment = plant.equipment_for_units(runtime.units)
        by_id = plant.equipment_by_id()
        self.tags: list[_Tag] = []          # 구성안 순서 (출력 순서)
        for e in self.equipment:
            driver = by_id[e.driven_by] if e.driven_by else None
            driver_speed = next((t for t in driver.tags if t.signal == "speed"), None) if driver else None
            for t in e.tags:
                dk = (driver.equip_id, driver_speed.tag_id) if driver_speed and t.signal == "speed" else None
                self.tags.append(_Tag(
                    (e.equip_id, t.tag_id), t.control.mean, t.control.sigma, t.decimals,
                    t.interval_sec or runtime.sample_interval_sec, RHO_TEMP if "temp" in t.signal else RHO,
                    # 피구동 speed 는 구동기의 난수열을 써서, 구동기가 다른 프로세스에 있어도 같은 값을 낸다
                    driver_speed.seed_key if dk else t.seed_key, dk))
        self._order = [t for t in self.tags if t.driver is None] + [t for t in self.tags if t.driver is not None]
        self._drivers = {t.driver for t in self.tags if t.driver is not None}
        self._responses: dict[tuple[str, int], dict[str, Response]] = {}
        self._opened: set[str] = set()
        self._restore: tuple[int | None, int] | None = None     # (복원 뒤 첫 시각의 KST 날짜, 그 epoch). 날짜 None = 첫 창 대기

    def restore(self, last_values: dict[TagKey, list[float]]) -> None:
        """태그별 최근 값(오래된 것부터)으로 판정 상태와 AR 직전 z 를 되살린다. 난수는 다음 창 첫 시각으로 다시 시드한다."""
        for t in self.tags:
            values = last_values.get(t.key)
            if values:
                zs = [zscore(v, t.mu, t.sigma) for v in values]
                t.nelson, t.zp = NelsonState(zs), zs[-1]
            t.day = None
        self._restore = (None, 0)

    def generate(self, start: datetime, end: datetime) -> list[Sample]:
        """[start, end) 구간, 이 프로세스가 맡은 설비 전체의 샘플. 측정 시각 순(같은 시각은 구성안 순)."""
        s0, s1 = int(start.timestamp()), int(end.timestamp())
        epochs = range(s0, s1, self.runtime.sample_interval_sec)
        if not epochs:
            return []
        if self._restore is not None and self._restore[0] is None:
            self._restore = (_kst_day(s0), s0)
        tz = start.tzinfo
        stamps = [datetime.fromtimestamp(ep, tz) for ep in epochs]
        positions: dict[int, list[int]] = {}
        for t in self.tags:
            if t.interval not in positions:
                positions[t.interval] = [i for i, ep in enumerate(epochs) if ep % t.interval == 0]

        windows = self._windows(s0, s1, epochs, positions)
        speeds: dict[TagKey, dict[int, float]] = {k: {} for k in self._drivers}
        columns: dict[TagKey, list[Sample]] = {}
        for t in self._order:
            pos = positions[t.interval]
            copy = speeds.get(t.driver) if t.driver else None
            columns[t.key] = self._series(t, pos, epochs, stamps, windows.get(t.key, ()), copy, speeds.get(t.key))

        rows: list[list[Sample]] = [[] for _ in epochs]     # 측정 시각 위치별, 구성안 순
        for t in self.tags:
            for i, s in zip(positions[t.interval], columns[t.key]):
                rows[i].append(s)
        return [s for r in rows for s in r]

    def _windows(self, s0: int, s1: int, epochs: range, positions: dict[int, list[int]]):
        """태그별 반응 구간 [(시작, 끝, 사건, 반응)], 사건 시작이 늦은 것부터. 자동 이상이 처음 표본에 걸리면 source.opened 를 부른다."""
        # 자동 이상의 건너뜀·자르기와 끝난 고장의 회복이 창마다 같게 나오도록 멀리 되돌아가 묻는다
        events = list(self.source.external(s0 - EXTERNAL_LOOKBACK_SEC, s1))
        if self.spontaneous:
            for day in range(_kst_day(s0 - MAX_RECOVERY_SEC), _kst_day(s1 - 1) + 1):
                for e in self.equipment:
                    events.extend(_daily(self.runtime.seed, e.equip_id, day))
        mine = {e.equip_id for e in self.equipment}
        # 설비별 이 창의 마지막 측정 시각 (그 시각까지 시작한 사건은 표본에 걸린다)
        last: dict[str, int] = {}
        for t in self.tags:
            p = positions[t.interval]
            if p:
                last[t.key[0]] = max(last.get(t.key[0], s0 - 1), epochs[p[-1]])
        out: dict[TagKey, list[tuple[float, float, AnomalyEvent, Response]]] = {}
        for ev in resolve(events, self.source.stops()):
            if ev.equip_id not in mine or ev.start >= s1 or (ev.end is not None and ev.end + MAX_RECOVERY_SEC <= s0):
                continue
            if ev.origin == "spontaneous" and ev.event_id not in self._opened and ev.start <= last.get(ev.equip_id, -math.inf):
                self._opened.add(ev.event_id)
                self.source.opened(ev)
            key = (ev.equip_id, ev.rule)
            if key not in self._responses:
                self._responses[key] = responses(self.plant, ev.equip_id, ev.rule)
            end = math.inf if ev.end is None else ev.end
            for tag_id, r in self._responses[key].items():
                out.setdefault((ev.equip_id, tag_id), []).append((ev.start + r.lag_sec, end + max(r.recovery_sec, 0), ev, r))
        # 한 시각에 구간이 여럿 걸리면(멈춘 고장의 회복 꼬리 + 새로 시작한 사건) _series 는 첫 것을 쓰므로 새 사건이 앞에 오게 한다
        for w in out.values():
            w.sort(key=lambda x: x[2].start, reverse=True)
        return out

    def _series(self, t: _Tag, pos: list[int], epochs: range, stamps: list[datetime], windows,
                copy: dict[int, float] | None, record: dict[int, float] | None) -> list[Sample]:
        """한 태그의 이 창 표본. copy 는 구동기 speed 의 시각별 z, record 는 이 태그가 구동기일 때 z 를 적어 둘 곳."""
        mu, sg, d, iv, rho, scale = t.mu, t.sigma, t.decimals, t.interval, t.rho, t.scale
        peek, push, normal = t.nelson.peek, t.nelson.push, t.normal
        equip_id, tag_id = t.key
        construct = Sample.model_construct
        restore_day, restore_epoch = self._restore or (None, 0)
        seed = self.runtime.seed
        zp = t.zp
        out: list[Sample] = []
        for i in pos:
            ep = epochs[i]
            day = (ep + KST_OFFSET) // DAY_SEC + EPOCH_ORDINAL
            if day != t.day:
                t.reseed([seed, t.crc, day, restore_epoch] if day == restore_day else [seed, t.crc, day], day)
            z = None
            for lo, hi, ev, r in windows:
                if lo <= ep < hi:
                    z = tag_z(ev, r, ep, iv, t.uniform())
                    break
            if z is None and copy is not None:
                z = copy.get(ep)
            if z is not None:
                v = round(mu + sg * z, d)
                zq = (v - mu) / sg
            else:
                # 억제 표집: 반올림한 값이 규칙을 어기지 않는 후보를 고른다
                base = rho * (CLIP if zp > CLIP else -CLIP if zp < -CLIP else zp)
                for _ in range(TRIES):
                    v = round(mu + sg * (base + scale * normal()), d)
                    zq = (v - mu) / sg
                    if not peek(zq):
                        break
                else:
                    v = round(mu + sg * normal(), d)
                    zq = (v - mu) / sg
            if record is not None:
                record[ep] = zq
            zp = zq
            out.append(construct(equip_id=equip_id, tag_id=tag_id, ts=stamps[i], value=v,
                                 status="Bad" if push(zq) else "Good"))
        t.zp = zp
        return out
