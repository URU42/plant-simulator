"""계측 이상 층 (2026-11-19 사용자 확정): 설비 고장과 별개로 정상 운전에 섞이는 계측 문제.

보내는 측정값만 바꾸고 공정 값(계산 태그의 입력·억제 표집 상태)은 그대로 둔다 — 유량계가 튀어도 전류는 그대로다.
- spike: 한 태그의 표본 하나가 z_min~z_max σ 튄다 (부호 무작위)
- stuck: 한 태그가 시작 직전에 보낸 값으로 고정된다
- dropout: 한 설비의 모든 태그를 보내지 않는다 (표본이 없다)
- bias·drift: 한 태그에 일정한 편향(z σ) 또는 0 → z σ 로 선형 증가하는 편향을 더한다. 평가 배치에서 직접 넣을 때만 쓰며
  일과 일정(_make)은 만들지 않는다(2026-11-27, D13 계측 이상 평가 보강).
일정은 (seed, plant_id, KST 날짜)로만 정해진다. 같은 시드면 같고, Unit 을 나눠 돌려도, 수집 서버가 정답지를 만들 때도 같은 일정이 나온다.
빈도·크기는 synthetic 가정이며 실행 설정(instrument_faults)에서 바꾼다.
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np

from common.config_models import InstrumentFaults, PlantConfig
from common.sim_clock import KST

DAY_SEC = 86400
KST_OFFSET = 9 * 3600
CODE = 2                    # 정답지 코드: 1 정상, 0 비정상(설비 고장), 2 계측 이상
KIND = "계측 이상(설비 고장 아님)"
LABEL = {
    "spike": ("IM-SPIKE", "계측값 순간 튐 (노이즈·전송 오류 후보)", "계측기·배선·전송 상태 확인. 설비 점검 대상 아님"),
    "stuck": ("IM-STUCK", "계측값 고착 (계측기 고장·통신 정지 후보)", "계측기·통신 상태 확인. 설비 점검 대상 아님"),
    "dropout": ("IM-DROPOUT", "설비 계측값 송신 누락 (I/O·통신 장애 후보)", "통신·I/O 상태 확인. 설비 점검 대상 아님"),
    "bias": ("IM-BIAS", "계측값 일정 편향 (영점·교정 이상 후보)", "계측기 교정 확인. 설비 점검 대상 아님"),
    "drift": ("IM-DRIFT", "계측값 서서히 벗어남 (계측기 드리프트 후보)", "계측기 교정 확인. 설비 점검 대상 아님"),
}


@dataclass(frozen=True)
class InstrumentEvent:
    event_id: str
    kind: str                   # spike / stuck / dropout / bias / drift
    equip_id: str
    tag_ids: tuple[str, ...]
    start: int                  # epoch 초, [start, end)
    end: int
    z: float = 0.0              # spike·bias 크기, drift 끝 크기 (σ 배수, 부호 포함)


class InstrumentSchedule:
    """공장 하나의 계측 이상 일정. 하루치를 처음 물을 때 만들어 둔다."""

    def __init__(self, plant: PlantConfig, cfg: InstrumentFaults, seed: int, sample_interval_sec: int) -> None:
        self.plant, self.cfg, self.seed, self.base = plant, cfg, seed, sample_interval_sec
        self._days: dict[int, list[InstrumentEvent]] = {}

    def events(self, start: float, end: float) -> list[InstrumentEvent]:
        """[start, end) 와 겹치는 사건 (시작 순)."""
        out: list[InstrumentEvent] = []
        for d in range(_day(start) - 1, _day(end) + 1):      # 전날 시작해 넘어온 고착·누락도 본다
            out += [e for e in self._day_events(d) if e.start < end and e.end > start]
        return sorted(out, key=lambda e: (e.start, e.event_id))

    def _day_events(self, day: int) -> list[InstrumentEvent]:
        if day not in self._days:
            self._days[day] = self._make(day) if self.cfg.enabled else []
        return self._days[day]

    def _make(self, day: int) -> list[InstrumentEvent]:
        cfg, plant_id = self.cfg, self.plant.plant.plant_id
        # 시드는 옛 plant_id(PL-RHDS)로 계산: 2026-11-27 에 접두사를 뗐지만 저장된 원시값·정답지와 같은 일정을 유지한다
        rng = np.random.default_rng([self.seed, zlib.crc32(f"PL-{plant_id}".encode()), day, 2])
        day0 = int(datetime.combine(date.fromordinal(day), datetime.min.time(), KST).timestamp())
        raw: list[tuple[str, str, tuple[str, ...], int, int, float]] = []

        def at(step: int) -> int:           # 하루 안에서 step 격자에 맞춘 시각
            return day0 + int(rng.integers(0, DAY_SEC // step)) * step

        for e in self.plant.equipment:
            for t in e.tags:
                step = t.interval_sec or self.base
                for _ in range(rng.poisson(cfg.spike.per_tag_per_day)):
                    s = at(step)
                    z = float(rng.uniform(cfg.spike.z_min, cfg.spike.z_max)) * (1 if rng.random() < 0.5 else -1)
                    raw.append(("spike", e.equip_id, (t.tag_id,), s, s + step, z))
            for _ in range(rng.poisson(cfg.stuck.per_equipment_per_week / 7)):
                t = e.tags[int(rng.integers(len(e.tags)))]
                step = t.interval_sec or self.base
                s = at(step)
                dur = int(rng.integers(cfg.stuck.min_sec, cfg.stuck.max_sec + 1)) // step * step or step
                raw.append(("stuck", e.equip_id, (t.tag_id,), s, s + dur, 0.0))
        for _ in range(rng.poisson(cfg.dropout.per_plant_per_day)):
            e = self.plant.equipment[int(rng.integers(len(self.plant.equipment)))]
            s = at(self.base)
            dur = int(rng.integers(cfg.dropout.min_sec, cfg.dropout.max_sec + 1)) // self.base * self.base or self.base
            raw.append(("dropout", e.equip_id, tuple(t.tag_id for t in e.tags), s, s + dur, 0.0))
        raw.sort(key=lambda r: (r[3], r[1], r[2]))
        stamp = date.fromordinal(day).strftime("%Y%m%d")
        return [InstrumentEvent(f"IM-{plant_id}-{stamp}-{n:04d}", *r) for n, r in enumerate(raw, 1)]


def _day(epoch: float) -> int:
    """epoch 초의 KST 날짜 ordinal."""
    return (date(1970, 1, 1) + timedelta(seconds=int(epoch) + KST_OFFSET)).toordinal()
