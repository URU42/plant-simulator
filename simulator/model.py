"""정상 운전 신호 생성기와 고장 모드 효과 합성.

태그마다 측정 시각 순으로 z(운전점 μ·σ 대비 편차)를 만들고 값 = round(μ + σ·z) 로 보낸다.
- 정상: z = 공통 요인 + 개별 AR(1) 잡음. 공통 요인은 공정 그룹마다 하루치 표준정규 수열로, (seed, 그룹, KST 날짜)로 정해져
  Unit 을 나눠 돌려도 같은 값이다. 개별 잡음은 규칙 1~3 위반이 생기는 후보를 다시 뽑아(억제 표집, 최대 100번) 조용하다.
  runtime.quiet_normal 이 false 면 억제 표집을 하지 않아 정상에서도 우연한 위반이 난다(학습·오탐률 평가용).
- 고장 모드(주입한 사건): 정상 z 위에 효과 z 를 더한다. 교호작용(boost)이 있으면 같은 설비 by 태그의 사건 효과만큼 키운다. 이때 억제 표집은 정상 부분만 보므로 위반은 효과 때문에만 생긴다.
  nelson_quiet 단계에서는 효과를 더한 값으로 판정해 위반이 나지 않게 고른다.
- 피구동 설비의 speed 태그는 구동기 speed 의 z 를 그대로 쓴다(축 공유).
- 계산 태그(derive)는 그 시각 입력 태그 값의 계산식 + 자체 잡음 + 효과다. 억제 표집은 자체 잡음만 다시 뽑으므로 위반 0 을 보장하지 않는다.
- 계측 이상 층(instrument_faults, 켰을 때): 보내는 값만 튐·고착·누락시킨다. 공정 값(계산 입력·판정 상태)은 그대로다.
태그마다 최근 z 와 그날의 난수열을 상태로 들고 창에서 창으로 이어 가며, restore 로 수집 서버의 마지막 값에서 다시 시작한다.
이상 판정은 탐지 쪽 몫이라 표본에 상태 칸이 없다 (누락은 표본을 보내지 않는다).
"""
from __future__ import annotations

import math
import zlib
from datetime import date, datetime
from functools import lru_cache
from typing import Protocol

import numpy as np

from common.config_models import Derive, PlantConfig, RuntimeConfig, TagKey
from common.message import Sample
from common.nelson import NelsonState, zscore
from simulator.fault import MAX_RECOVERY_SEC, FaultEvent, TagPlan, boosted, event_tag_effect, plan
from simulator.instrument import InstrumentEvent, InstrumentSchedule
from simulator.physics_adapter import load_physics

RHO = 0.12                  # 개별 잡음 AR(1) 계수
RHO_TEMP = 0.25             # 온도 신호의 AR(1) 계수
CLIP = 2.0                  # AR 에 넣는 직전 잡음의 상·하한
TRIES = 100                 # 억제 표집의 최대 시도 수
BATCH = 256                 # 난수를 한 번에 뽑는 개수
DAY_SEC = 86400
KST_OFFSET = 9 * 3600
EPOCH_ORDINAL = date(1970, 1, 1).toordinal()


class EventSource(Protocol):
    """생성기 밖에서 오는 사건(주입한 고장 모드). external 은 [start, end) 에 효과가 남아 있을 수 있는 사건을 모두 돌려준다
    (시작 < end 이고, 안 멈췄거나 멈춤 + 회복 > start, 손상이 남았으면 안 고쳤거나 수리 + 회복 > start)."""

    def external(self, start: float, end: float) -> list[FaultEvent]: ...


class NoEvents:
    """아무 사건도 없는 EventSource."""

    def external(self, start: float, end: float) -> list[FaultEvent]:
        return []


@lru_cache(maxsize=64)
def _factor(seed: int, group: str, day: int) -> np.ndarray:
    """공정 그룹의 KST 하루치 공통 요인 (초 단위 표준정규)."""
    return np.random.default_rng([seed, zlib.crc32(group.encode()), day]).standard_normal(DAY_SEC)


class _Tag:
    """한 태그의 고정 값(기준·주기·AR 계수·공통 요인 가중)과 생성 상태(판정 상태, 직전 잡음, 그날의 난수)."""

    __slots__ = ("key", "mu", "sigma", "decimals", "interval", "rho", "scale", "crc", "driver", "group", "load", "own",
                 "derive", "inputs", "ref", "nelson", "ep", "rng", "day", "normals")

    def __init__(self, key: TagKey, mu: float, sigma: float, decimals: int, interval: int, rho: float,
                 seed_key: str, driver: TagKey | None, group: str, load: float, derive: Derive | None = None) -> None:
        self.key = key
        self.mu, self.sigma, self.decimals, self.interval = mu, sigma, decimals, interval
        self.rho, self.scale = rho, math.sqrt(1 - rho * rho)
        self.crc = zlib.crc32(seed_key.encode())
        self.driver = driver                # 피구동 speed 태그면 구동기 speed 태그의 키
        self.group = group                  # 공통 요인을 나눠 갖는 공정 그룹
        self.load = load                    # 공통 요인 가중 (부호 × √share). 참여하지 않으면 0
        self.own = math.sqrt(1 - load * load)   # 개별 잡음 가중. 전체 분산이 1 이 되게
        self.derive = derive                # 계산 태그면 계산식
        self.inputs = [(key[0], i) for i in derive.inputs] if derive else []
        self.ref: tuple[list[float], float] = ([], 0.0)     # 계산식의 설계점 (입력 μ, 출력 μ). PlantGenerator 가 채운다
        self.nelson = NelsonState()
        self.ep = 0.0                       # 직전 개별 잡음
        self.rng: np.random.Generator | None = None
        self.day: int | None = None         # rng 를 시드한 KST 날짜
        self.normals: list[float] = []

    def reseed(self, seed: list[int], day: int) -> None:
        self.rng = np.random.default_rng(seed)
        self.day = day
        self.normals = []

    def normal(self) -> float:
        if not self.normals:
            self.normals = self.rng.standard_normal(BATCH).tolist()
        return self.normals.pop()


class PlantGenerator:
    def __init__(self, plant: PlantConfig, runtime: RuntimeConfig, source: EventSource | None = None) -> None:
        runtime.validate_tag_intervals(plant)
        self.plant = plant
        self.runtime = runtime
        self.source = source or NoEvents()
        self.physics = load_physics(plant, runtime.physics) if runtime.physics is not None else None
        self.equipment = plant.equipment_for_units(runtime.units)
        self.control = plant.control_by_key()
        by_id = plant.equipment_by_id()
        group_of = plant.unit_group_of()
        corr = plant.correlation
        self.tags: list[_Tag] = []          # 구성안 순서 (출력 순서)
        for e in self.equipment:
            driver = by_id[e.driven_by] if e.driven_by else None
            driver_speed = next((t for t in driver.tags if t.signal == "speed"), None) if driver else None
            for t in e.tags:
                dk = (driver.equip_id, driver_speed.tag_id) if driver_speed and t.signal == "speed" else None
                # 피구동 speed 는 구동기의 난수열·공통 요인을 써서, 구동기가 다른 프로세스에 있어도 같은 z 를 낸다
                src_e, src_t = (driver, driver_speed) if dk else (e, t)
                sign = corr.signs.get(src_t.signal, 0) if corr else 0
                mu, sigma = self.control[(e.equip_id, t.tag_id)]
                self.tags.append(_Tag(
                    (e.equip_id, t.tag_id), mu, sigma, t.decimals,
                    t.interval_sec or runtime.sample_interval_sec, RHO_TEMP if "temp" in t.signal else RHO,
                    src_t.seed_key, dk, group_of[src_e.unit_id], sign * math.sqrt(corr.share) if sign else 0.0, t.derive))
        design = plant.tag_by_key()
        for t in self.tags:
            if t.derive is not None:
                t.ref = ([design[k].control.mean for k in t.inputs], design[t.key].control.mean)
        plain = [t for t in self.tags if t.driver is None and t.derive is None]
        self._order = plain + [t for t in self.tags if t.driver is not None] + [t for t in self.tags if t.derive is not None]
        self._recorded = {t.driver for t in self.tags if t.driver is not None} | {k for t in self.tags for k in t.inputs}
        self._plans: dict[str, dict[TagKey, TagPlan]] = {}      # event_id → 태그별 적용
        self._restore: tuple[int | None, int] | None = None     # (복원 뒤 첫 시각의 KST 날짜, 그 epoch). 날짜 None = 첫 창 대기
        cfg = runtime.instrument_faults
        self.instrument = InstrumentSchedule(plant, cfg, runtime.seed, runtime.sample_interval_sec) if cfg.enabled else None
        self._sent: dict[TagKey, float] = {}       # 태그별 마지막으로 보낸 값 (고착이 붙잡는 값)
        self._held: dict[str, float] = {}          # 고착 사건별 붙잡은 값

    def restore(self, last_values: dict[TagKey, list[float]]) -> None:
        """태그별 최근 값(오래된 것부터)으로 판정 상태와 직전 잡음을 되살린다. 난수는 다음 창 첫 시각으로 다시 시드한다."""
        for t in self.tags:
            values = last_values.get(t.key)
            if values:
                if self.physics is not None and t.key in self.physics.tag_keys:
                    # 과거 계측값에는 유압 상태가 섞여 있으므로 잡음 상태로 복원하지 않는다.
                    t.nelson, t.ep = NelsonState(), 0.0
                else:
                    zs = [zscore(v, t.mu, t.sigma) for v in values]
                    t.nelson, t.ep = NelsonState(zs), zs[-1]
                self._sent[t.key] = values[-1]
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

        events = self.source.external(s0 - MAX_RECOVERY_SEC, s1)
        active = self._active(events)
        centers: dict[TagKey, dict[int, float]] = {}
        if self.physics is not None and self.physics.tag_keys.intersection(t.key for t in self.tags):
            plans = self.physics.parameter_plans(events)
            centers = {key: {} for key in self.physics.tag_keys}
            for ep in epochs:
                for key, center in self.physics.sensor_centers(self.physics.state(ep, plans)).items():
                    centers[key][ep] = center
        values: dict[TagKey, dict[int, float]] = {k: {} for k in self._recorded}    # 구동기 speed·계산 입력의 시각별 값
        columns: dict[TagKey, list[Sample]] = {}
        for t in self._order:
            if t.derive is not None:
                columns[t.key] = self._derived(t, positions[t.interval], epochs, stamps, active.get(t.key, ()), active,
                                               [values[k] for k in t.inputs], values.get(t.key), centers.get(t.key))
                continue
            copy = None
            if t.driver:
                dmu, dsg = self.control[t.driver]
                copy = {ep: (v - dmu) / dsg for ep, v in values[t.driver].items()}
            columns[t.key] = self._series(t, positions[t.interval], epochs, stamps, active.get(t.key, ()), active, copy,
                                          values.get(t.key), centers.get(t.key))

        measure = self._measure(s0, s1)
        rows: list[list[Sample]] = [[] for _ in epochs]     # 측정 시각 위치별, 구성안 순
        for t in self.tags:
            for i, s in zip(positions[t.interval], columns[t.key]):
                if measure is not None:
                    s = measure(t, epochs[i], s)
                    if s is None:
                        continue
                rows[i].append(s)
        return [s for r in rows for s in r]

    def _measure(self, s0: int, s1: int):
        """이 창의 계측 이상을 보내는 표본에 입히는 함수 (꺼져 있으면 None). 표본을 바꾸거나, 누락이면 None 을 돌려준다."""
        if self.instrument is None:
            return None
        by_tag: dict[TagKey, list[InstrumentEvent]] = {}
        for ev in self.instrument.events(s0, s1):
            for tag_id in ev.tag_ids:
                by_tag.setdefault((ev.equip_id, tag_id), []).append(ev)
        self._held = {k: v for k, v in self._held.items() if any(k == ev.event_id for evs in by_tag.values() for ev in evs)}

        def apply(t: _Tag, ep: int, s: Sample) -> Sample | None:
            v = s.value
            for ev in by_tag.get(t.key, ()):
                if not ev.start <= ep < ev.end:
                    continue
                if ev.kind == "dropout":
                    return None
                if ev.kind == "stuck":
                    v = self._held.setdefault(ev.event_id, self._sent.get(t.key, v))
                else:
                    v = round(v + ev.z * t.sigma, t.decimals)
            self._sent[t.key] = v
            return s if v == s.value else Sample.model_construct(equip_id=s.equip_id, tag_id=s.tag_id, ts=s.ts, value=v)
        return apply

    def _active(self, events: list[FaultEvent]) -> dict[TagKey, list[tuple[FaultEvent, TagPlan]]]:
        """이 창에 효과가 있을 수 있는 사건의 태그별 적용 목록 (이 프로세스가 맡은 태그만)."""
        out: dict[TagKey, list[tuple[FaultEvent, TagPlan]]] = {}
        mine = {t.key for t in self.tags}
        for ev in events:
            if ev.mode.physics_only and self.physics is None:
                continue
            if ev.event_id not in self._plans:
                self._plans[ev.event_id] = (self.physics.tag_plan(ev.mode, ev.equip_id) if self.physics
                                           else plan(self.plant, ev.mode, ev.equip_id, self.control))
            for key, tp in self._plans[ev.event_id].items():
                if key in mine and tp.via != "physics":
                    out.setdefault(key, []).append((ev, tp))
        return out

    def _series(self, t: _Tag, pos: list[int], epochs: range, stamps: list[datetime], active, everything,
                copy: dict[int, float] | None, record: dict[int, float] | None,
                centers: dict[int, float] | None = None) -> list[Sample]:
        """한 태그의 이 창 표본. active 는 이 태그, everything 은 모든 태그의 사건 적용(교호작용의 by 태그를 볼 때). copy 는 구동기 speed 의 시각별 z, record 는 이 태그 값을 시각별로 적어 둘 곳(구동기·계산 입력)."""
        mu, sg, d, rho, scale, load, own = t.mu, t.sigma, t.decimals, t.rho, t.scale, t.load, t.own
        safe, bounds, push, normal = self._safe(t), t.nelson.bounds, t.nelson.push, t.normal
        equip_id, tag_id = t.key
        construct = Sample.model_construct
        seed = self.runtime.seed
        factor, fday = None, None
        ep_prev = t.ep
        out: list[Sample] = []
        for i in pos:
            ep = epochs[i]
            day = self._day(t, ep)
            center = centers[ep] if centers is not None else mu
            fx, quiet = _effect(active, ep, everything)
            if copy is not None and ep in copy:
                z = copy[ep]
                v = round(mu + sg * z, d)
                zq = (v - center) / sg
                push(zq)
            else:
                if load and day != fday:
                    factor, fday = _factor(seed, t.group, day), day
                common = load * float(factor[(ep + KST_OFFSET) % DAY_SEC]) if load else 0.0
                base = rho * (CLIP if ep_prev > CLIP else -CLIP if ep_prev < -CLIP else ep_prev)
                total = fx == 0.0 or quiet      # 효과가 없거나 nelson_quiet 이면 보낼 값으로, 아니면 정상 부분만으로 판정한다
                for _ in range(TRIES):
                    e = base + scale * normal()
                    zn = common + own * e
                    v = round(center + sg * (zn + fx), d)
                    zq = (v - center) / sg
                    zc = zq if total else zn
                    if safe(zc):
                        break
                else:
                    # 뽑기로 못 찾으면 허용 구간 안의 값을 직접 정한다 (공통 요인이 클 때 개별 잡음만으로는 반대쪽에 못 닿는다)
                    lo, hi = bounds()
                    q = 10 ** -d / sg if total else 0.0       # 반올림으로 구간 밖에 나가지 않게 한 칸 안쪽
                    zc = min(max(zc, lo + q), hi - q) if hi - lo > 2 * q else (lo + hi) / 2
                    zn = zc - fx if total else zc
                    e = (zn - common) / own
                    v = round(center + sg * (zn + fx), d)
                    zq = (v - center) / sg
                    zc = zq if total else zn
                push(zc)
                ep_prev = e
            if record is not None:
                record[ep] = v
            out.append(construct(equip_id=equip_id, tag_id=tag_id, ts=stamps[i], value=v))
        t.ep = ep_prev
        return out

    def _derived(self, t: _Tag, pos: list[int], epochs: range, stamps: list[datetime], active, everything,
                 ins: list[dict[int, float]], record: dict[int, float] | None,
                 centers: dict[int, float] | None = None) -> list[Sample]:
        """계산 태그의 이 창 표본: 그 시각 입력 값의 계산식 + 자체 잡음(iid) + 효과. 자체 잡음만 TRIES 번 다시 뽑고,
        못 찾으면 마지막 값을 쓴다 (입력이 정한 몫은 바꿀 수 없어 위반 0 을 보장하지 않는다)."""
        mu, sg, d, noise, value = t.mu, t.sigma, t.decimals, t.derive.noise_sigma, t.derive.value
        ref, ref_out = t.ref
        safe, push, normal = self._safe(t), t.nelson.push, t.normal
        equip_id, tag_id = t.key
        out: list[Sample] = []
        for i in pos:
            ep = epochs[i]
            self._day(t, ep)
            fx, quiet = _effect(active, ep, everything)
            base = centers[ep] if centers is not None else value([x[ep] for x in ins], ref, ref_out)
            center = base if centers is not None else mu
            total = fx == 0.0 or quiet
            for _ in range(TRIES):
                n = noise * normal()
                v = round(base + n + sg * fx, d)
                zc = (v - center) / sg if total else (base + n - center) / sg
                if safe(zc):
                    break
            push(zc)
            if record is not None:
                record[ep] = v
            out.append(Sample.model_construct(equip_id=equip_id, tag_id=tag_id, ts=stamps[i], value=v))
        return out

    def _safe(self, t: _Tag):
        """억제 표집의 판정. quiet_normal 이 꺼져 있으면 첫 후보를 그대로 쓴다(nelson_quiet 단계도 거르지 않는다)."""
        return t.nelson.safe if self.runtime.quiet_normal else _accept

    def _day(self, t: _Tag, ep: int) -> int:
        """ep 의 KST 날짜. 날짜가 바뀌면 그날의 난수열로 다시 시드한다(복원한 날은 복원 시각도 넣는다)."""
        day = (ep + KST_OFFSET) // DAY_SEC + EPOCH_ORDINAL
        if day != t.day:
            restore_day, restore_epoch = self._restore or (None, 0)
            seed = self.runtime.seed
            t.reseed([seed, t.crc, day, restore_epoch] if day == restore_day else [seed, t.crc, day], day)
        return day


def _accept(z: float) -> bool:
    return True


def _effect(active, ep: int, everything=None) -> tuple[float, bool]:
    """ep 에서 걸린 사건들의 (효과 z 합, nelson_quiet 여부). 교호작용이 있는 적용은 그 시각 by 태그의 사건 효과 z(모든 사건 합,
    정상 잡음 제외)로 배율을 정한다. by 태그에는 교호작용이 없어 한 단계로 끝난다."""
    fx, quiet = 0.0, False
    for ev, tp in active:
        z, q = event_tag_effect(tp, ev, ep)
        if tp.boost is not None and z:
            z = boosted(tp.boost, z, _effect((everything or {}).get(tp.boost.by, ()), ep)[0], ep - ev.start, ev.speed)
        fx += z
        quiet = quiet or q
    return fx, quiet


def _kst_day(epoch: float) -> int:
    """epoch 초의 KST 날짜 ordinal."""
    return int(epoch + KST_OFFSET) // DAY_SEC + EPOCH_ORDINAL
