"""사건 장부 (Nelson 이상 설계 §4.5–4.6): 수집 서버 DB 의 anomaly_event 에 사건을 적고 생성기에 외부 사건을 알려 주는 EventSource.

주입 사건은 API 가, 지속 고장은 실시간 시작부터 interval 마다 장부가 열고, 자동 이상은 생성기가 처음 표본을 만들 때 opened 로 알린다.
세 가지 모두 한 테이블에 두어 정답지가 되고, 재시작하면 테이블에서 다시 읽어 열린 주입·지속 고장을 이어 간다.
멈춤은 사건의 끝이 아니라 stop_requested_at 으로 따로 적어 stops 로 알린다. 생성기 워커 스레드와 요청 핸들러가 함께 부르므로
장부 상태는 자체 락으로 지킨다.
"""
from __future__ import annotations

import json
import math
import threading
from datetime import datetime, timezone

import numpy as np

from collector.storage import Storage
from common.config_models import PlantConfig
from common.timefmt import from_db, to_db
from simulator.anomaly import AnomalyEvent, classify, responses

RULES = range(1, 9)


class Busy(Exception):
    """그 설비에 주입·지속 고장이 아직 살아 있다 (먼저 멈춰야 한다)."""


def _db(epoch: float) -> str:
    return to_db(datetime.fromtimestamp(epoch, timezone.utc))


def _epoch(s: str | None) -> float | None:
    return None if s is None else from_db(s).timestamp()


class AnomalyBook:
    def __init__(self, storage: Storage, plant: PlantConfig, seed: int, interval_min: float) -> None:
        self.storage = storage
        self.plant = plant
        self.plant_id = plant.plant.plant_id
        self.seed = seed
        self.interval = interval_min * 60                   # 지속 고장 간격(초). 0 이면 열지 않는다
        self.equip_ids = [e.equip_id for e in plant.equipment]     # 구성안 순서 (지속 고장 후보의 순서)
        self._lock = threading.RLock()
        self._events: dict[str, AnomalyEvent] = {}         # 장부의 모든 사건 (자동 이상은 end = 예정된 끝)
        self._stops: dict[str, float] = {}                 # event_id → 멈춤 시각
        self._next_fault: float | None = None              # 다음 지속 고장 예정 시각. 실시간 전에는 None
        for r in storage.query_dicts("SELECT * FROM anomaly_event ORDER BY start_at"):
            self._events[r["event_id"]] = AnomalyEvent(r["event_id"], r["origin"], r["equip_id"], r["rule"],
                                                       _epoch(r["start_at"]), _epoch(r["scheduled_end_at"]),
                                                       bool(r["normal_change"]))
            if r["stop_requested_at"] is not None:
                self._stops[r["event_id"]] = _epoch(r["stop_requested_at"])

    # --- EventSource ---------------------------------------------------------
    def set_live(self, at: float) -> None:
        """실시간 시작. 첫 지속 고장은 max(at, 마지막 지속 고장 시작) + interval."""
        with self._lock:
            if self.interval <= 0:
                return
            last = max((e.start for e in self._events.values() if e.origin == "persistent"), default=at)
            self._next_fault = max(at, last) + self.interval

    def external(self, start: float, end: float) -> list[AnomalyEvent]:
        """[start, end) 와 겹치는 주입·지속 고장. 예정 시각이 end 이하인 지속 고장은 이때 연다."""
        with self._lock:
            while self._next_fault is not None and self._next_fault <= end:
                self._open_fault(self._next_fault)
                self._next_fault += self.interval
            return [e for e in self._events.values() if e.origin != "spontaneous" and e.start < end
                    and self._stops.get(e.event_id, math.inf) > start]

    def stops(self) -> dict[str, float]:
        with self._lock:
            return dict(self._stops)

    def opened(self, event: AnomalyEvent) -> None:
        """자동 이상이 처음 표본에 걸렸다. 같은 event_id 는 한 번만 적는다."""
        with self._lock:
            if event.event_id not in self._events:
                self._insert(event, ignore=True)

    # --- 주입·멈춤 -------------------------------------------------------------
    def inject(self, equip_id: str, rule: int, at: float) -> AnomalyEvent:
        """at 부터 멈출 때까지 가는 주입 사건을 연다. 진행 중인 자동 이상은 at 에서 멈춘다."""
        with self._lock:
            if self._faulty(equip_id, at):
                raise Busy(equip_id)
            n = 1 + sum(e.origin == "injected" for e in self._events.values())
            event = AnomalyEvent(f"IN-{self.plant_id}-{n:04d}", "injected", equip_id, rule, at, None)
            self._supersede(equip_id, at)
            self._insert(event)
            return event

    def stop(self, equip_id: str | None, at: float) -> list[str]:
        """그 설비(None 이면 전체)의 살아 있는 사건(아직 안 멈춘 주입·지속 고장, at 에 진행 중인 자동 이상)을 at 에서 멈춘다."""
        with self._lock:
            ids = [e.event_id for e in self._events.values()
                   if (equip_id is None or e.equip_id == equip_id) and self._alive(e, at)]
            for event_id in ids:
                self._set_stop(event_id, at)
            return ids

    # --- 정답지 ---------------------------------------------------------------
    def answer_key(self, equip_id: str | None, from_: str | None, to: str | None) -> list[dict]:
        """사건 목록(시작 순). from/to(UTC ISO 문자열)는 구간과 겹치는 사건: 시작 < to 이고 끝이 없거나 끝 ≥ from."""
        where, params = ["1=1"], []
        if equip_id:
            where.append("equip_id = ?"); params.append(equip_id)
        if to:
            where.append("start_at < ?"); params.append(to)
        rows = self.storage.query_dicts(f"SELECT * FROM anomaly_event WHERE {' AND '.join(where)} ORDER BY start_at, event_id",
                                        params)
        out = []
        for r in rows:
            tags = json.loads(r["response_tags"])
            ends = [_epoch(x) for x in (r["scheduled_end_at"], r["stop_requested_at"]) if x is not None]
            recovery = max((resp.recovery_sec for resp in responses(self.plant, r["equip_id"], r["rule"]).values()), default=0)
            end_at = _db(min(ends) + recovery) if ends else None
            if from_ and end_at is not None and end_at < from_:
                continue
            out.append({"event_id": r["event_id"], "origin": r["origin"], "equip_id": r["equip_id"], "rule": r["rule"],
                        "kind": r["kind"], "cause": r["cause"], "action": r["action"], "response_tags": tags,
                        "start_at": r["start_at"], "stop_requested_at": r["stop_requested_at"], "end_at": end_at})
        return out

    # --- 내부 (락을 쥔 채로 부른다) ---------------------------------------------
    def _alive(self, e: AnomalyEvent, at: float) -> bool:
        """멈출 수 있는 사건인가: 주입·지속 고장은 아직 안 멈췄으면, 자동 이상은 at 에 진행 중이고 안 멈췄으면."""
        if e.event_id in self._stops:
            return False
        return e.origin != "spontaneous" or e.start <= at < e.end

    def _faulty(self, equip_id: str, at: float) -> bool:
        """그 설비에 at 에 살아 있는(안 멈췄거나 at 뒤에 멈추는) 주입·지속 고장이 있는가."""
        return any(e.equip_id == equip_id and e.origin != "spontaneous" and self._stops.get(e.event_id, math.inf) > at
                   for e in self._events.values())

    def _supersede(self, equip_id: str, at: float) -> None:
        """고장이 시작하는 at 에 그 설비에서 진행 중인 자동 이상을 멈춘다 (정답지의 끝이 생성기의 자르기와 맞도록)."""
        for e in list(self._events.values()):
            if e.equip_id == equip_id and e.origin == "spontaneous" and self._alive(e, at):
                self._set_stop(e.event_id, at)

    def _open_fault(self, at: float) -> None:
        """at 에 지속 고장을 연다. 고장 없는 설비가 없으면 이 회차는 건너뛴다."""
        free = [x for x in self.equip_ids if not self._faulty(x, at)]
        if not free:
            return
        n = 1 + sum(e.origin == "persistent" for e in self._events.values())
        rng = np.random.default_rng([self.seed, n])
        equip_id = free[int(rng.integers(len(free)))]
        rule = int(rng.integers(RULES.start, RULES.stop))
        self._supersede(equip_id, at)
        self._insert(AnomalyEvent(f"PF-{self.plant_id}-{n:04d}", "persistent", equip_id, rule, at, None))

    def _insert(self, e: AnomalyEvent, ignore: bool = False) -> None:
        kind, cause, action = classify(e.rule, e.normal_change)
        tags = list(responses(self.plant, e.equip_id, e.rule))
        self.storage.execute(
            f"INSERT {'OR IGNORE ' if ignore else ''}INTO anomaly_event (event_id, origin, equip_id, rule, normal_change, kind, "
            "cause, action, response_tags, start_at, scheduled_end_at, stop_requested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (e.event_id, e.origin, e.equip_id, e.rule, int(e.normal_change), kind, cause, action, json.dumps(tags, ensure_ascii=False),
             _db(e.start), None if e.end is None else _db(e.end)))
        self._events[e.event_id] = e

    def _set_stop(self, event_id: str, at: float) -> None:
        self.storage.execute("UPDATE anomaly_event SET stop_requested_at = ? WHERE event_id = ?", (_db(at), event_id))
        self._stops[event_id] = at
