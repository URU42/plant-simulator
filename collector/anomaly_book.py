"""사건 장부: 수집 서버 DB 의 anomaly_event 에 주입한 고장 모드를 적고 생성기에 외부 사건을 알려 주는 EventSource.

고장 모드 사건은 API 로 주입한 것뿐이며 이 테이블이 정답지다. 계측 이상(켰을 때)은 생성기와 같은 일정을 다시 계산해 정답지에 함께 싣는다.
정답지 코드(code): 1 정상(정상 운전 변화), 0 비정상(설비 고장), 2 계측 이상. 사건이 없는 구간은 정상(1)이다. 주입할 때 태그별 적용(단계·효과 시작)을
plan JSON 으로 함께 적어, 나중에 fault_modes.yaml 이 바뀌어도 정답지는 주입 당시 그대로 남는다.
재시작하면 테이블에서 다시 읽어 안 멈춘 사건을 이어 간다. 생성기 워커 스레드와 요청 핸들러가 함께 부르므로 자체 락으로 지킨다.
생성기가 이미 물어본 구간 끝(high-water)보다 앞선 주입·멈춤은 그 끝으로 미뤄 이미 만든 표본을 건드리지 않는다.
"""
from __future__ import annotations

import json
import logging
import math
import threading
from dataclasses import replace
from datetime import datetime, timezone

from collector.storage import Storage
from common.config_models import PlantConfig
from common.timefmt import from_db, to_db
from simulator.fault import RECOVERY_SEC, SPEEDS, FaultEvent, FaultMode, effect_start, plan
from simulator import instrument as im

log = logging.getLogger("collector.anomaly_book")


class Busy(Exception):
    """그 설비에 안 멈춘 주입 사건이 있다 (먼저 멈춰야 한다)."""


class NotApplicable(Exception):
    """모르는 고장 모드이거나, 그 설비에 걸 수 없는 고장 모드다."""


def _db(epoch: float) -> str:
    return to_db(datetime.fromtimestamp(epoch, timezone.utc))


def _epoch(s: str | None) -> float | None:
    return None if s is None else from_db(s).timestamp()


class AnomalyBook:
    def __init__(self, storage: Storage, plant: PlantConfig, modes: dict[str, FaultMode],
                 instrument: im.InstrumentSchedule | None = None) -> None:
        self.storage = storage
        self.instrument = instrument
        self.plant = plant
        self.plant_id = plant.plant.plant_id
        self.modes = modes
        self.control = plant.control_by_key()
        self._lock = threading.RLock()
        self._events: dict[str, FaultEvent] = {}
        self._high = -math.inf                  # 생성기가 external 로 물어본 구간 끝의 최대 (그 앞 표본은 이미 만들었다)
        for r in storage.query_dicts("SELECT * FROM anomaly_event ORDER BY start_at"):
            mode = modes.get(r["mode"])
            if mode is None:        # 파일에서 빠진 고장 모드: 정답지에는 남고 생성에는 더 쓰지 않는다
                log.warning("anomaly_event %s: unknown fault mode %s, not generated", r["event_id"], r["mode"])
                continue
            self._events[r["event_id"]] = FaultEvent(r["event_id"], r["equip_id"], mode, _epoch(r["start_at"]),
                                                     _epoch(r["stop_requested_at"]), json.loads(r["plan"]).get("speed", 1))

    def applicable(self, equip_id: str) -> list[FaultMode]:
        """그 설비에 걸 수 있는 고장 모드 (효과가 걸리는 태그가 하나라도 있는 것)."""
        e = self.plant.equipment_by_id()[equip_id]
        return [m for m in self.modes.values() if m.applies_to(e) and plan(self.plant, m, equip_id, self.control)]

    # --- EventSource ---------------------------------------------------------
    def external(self, start: float, end: float) -> list[FaultEvent]:
        with self._lock:
            self._high = max(self._high, end)
            return [e for e in self._events.values()
                    if e.start < end and (e.stop is None or e.stop + RECOVERY_SEC > start)]

    # --- 주입·멈춤 -------------------------------------------------------------
    def inject(self, equip_id: str, mode_id: str, at: float, speed: int = 1) -> FaultEvent:
        """at(이미 만든 구간 끝보다 앞서면 그 끝)부터 멈출 때까지 가는 사건을 연다. speed 는 진행 속도(SPEEDS)."""
        if speed not in SPEEDS:
            raise NotApplicable(f"speed must be one of {SPEEDS}")
        mode = self.modes.get(mode_id)
        if mode is None:
            raise NotApplicable(f"unknown fault mode {mode_id}")
        if not mode.applies_to(self.plant.equipment_by_id()[equip_id]):
            raise NotApplicable(f"fault mode {mode_id} does not apply to {equip_id}")
        tags = plan(self.plant, mode, equip_id, self.control)
        if not tags:
            raise NotApplicable(f"fault mode {mode_id} has no tag to move on {equip_id}")
        with self._lock:
            at = max(at, self._high)
            if any(e.equip_id == equip_id and (e.stop is None or e.stop > at) for e in self._events.values()):
                raise Busy(equip_id)
            n = 1 + self.storage.query("SELECT COUNT(*) FROM anomaly_event")[0][0]
            event = FaultEvent(f"FM-{self.plant_id}-{n:04d}", equip_id, mode, at, speed=speed)
            stages = [{"id": s.id, "name": s.name, "at_sec": s.at_sec, "nelson_quiet": s.nelson_quiet} for s in mode.stages]
            applied = [{"equip_id": k[0], "tag_id": k[1], "stage": a.stage, "start_sec": a.start}
                       for k, tp in tags.items() for a in tp.applied]
            self.storage.execute(
                "INSERT INTO anomaly_event (event_id, equip_id, mode, kind, cause, action, expect, plan, start_at, stop_requested_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (event.event_id, equip_id, mode.id, mode.label.kind, mode.label.cause, mode.label.action,
                 None if mode.expect is None else json.dumps(mode.expect, ensure_ascii=False),
                 json.dumps({"stages": stages, "tags": applied, "unobserved": mode.unobserved, "code": mode.label.code,
                             "speed": speed},
                            ensure_ascii=False), _db(at)))
            self._events[event.event_id] = event
            return event

    def stop(self, equip_id: str | None, at: float) -> tuple[float, list[str]]:
        """그 설비(None 이면 전체)의 안 멈춘 사건을 멈춘다. 멈춤 시각은 at(이미 만든 구간 끝보다 앞서면 그 끝)이며
        (멈춤 시각, 멈춘 event_id 목록)을 돌려준다."""
        with self._lock:
            at = max(at, self._high)
            ids = [e.event_id for e in self._events.values() if (equip_id is None or e.equip_id == equip_id) and e.stop is None]
            for event_id in ids:
                self.storage.execute("UPDATE anomaly_event SET stop_requested_at = ? WHERE event_id = ?", (_db(at), event_id))
                self._events[event_id] = replace(self._events[event_id], stop=at)
            return at, ids

    def active(self) -> list[FaultEvent]:
        """안 멈춘 사건 (시작 순). 화면이 설비마다 적용/해제를 고를 때 쓴다."""
        with self._lock:
            return sorted((e for e in self._events.values() if e.stop is None), key=lambda e: (e.start, e.event_id))

    # --- 정답지 ---------------------------------------------------------------
    def answer_key(self, equip_id: str | None, from_: str | None, to: str | None) -> list[dict]:
        """사건 목록(시작 순). from/to(UTC ISO 문자열)는 구간과 겹치는 사건: 시작 < to 이고 끝이 없거나 끝 ≥ from.
        끝(end_at)은 멈춤 + 회복. 단계·태그별 효과 시작은 절대 시각으로 펼치고, 멈추기 전에 시작하지 못한 것은 뺀다.
        시작 전이나 시작 시각에 멈춘 사건(표본에 나온 적 없다)은 싣지 않는다.
        계측 이상은 수집 서버에 데이터가 있는 구간(가장 이른 측정 ~ sim_now)과 겹치는 것만 싣는다."""
        where, params = ["1=1"], []
        if equip_id:
            where.append("equip_id = ?")
            params.append(equip_id)
        if to:
            where.append("start_at < ?")
            params.append(to)
        rows = self.storage.query_dicts(f"SELECT * FROM anomaly_event WHERE {' AND '.join(where)} ORDER BY start_at, event_id",
                                        params)
        out = []
        for r in rows:
            start, stop = _epoch(r["start_at"]), _epoch(r["stop_requested_at"])
            if stop is not None and stop <= start:
                continue
            end_at = None if stop is None else _db(stop + RECOVERY_SEC)
            if from_ and end_at is not None and end_at < from_:
                continue
            p = json.loads(r["plan"])
            speed = p.get("speed", 1)           # 단계 시각은 1/speed, lag 는 그대로 (simulator.fault.effect_start)
            stage_at = {s["id"]: s["at_sec"] for s in p["stages"]}

            def started(sec: float) -> bool:
                return stop is None or start + sec / speed < stop

            stages = [{"id": s["id"], "name": s["name"], "start_at": _db(start + s["at_sec"] / speed), "nelson_quiet": s["nelson_quiet"]}
                      for s in p["stages"] if started(s["at_sec"])]
            tags = [{"equip_id": t["equip_id"], "tag_id": t["tag_id"], "stage": t["stage"], "start_at": _db(start + sec)}
                    for t in p["tags"]
                    for sec in [effect_start(t["start_sec"], t["start_sec"] - stage_at[t["stage"]], speed)]
                    if stop is None or start + sec < stop]
            out.append({"event_id": r["event_id"], "equip_id": r["equip_id"], "mode": r["mode"],
                        "kind": r["kind"], "cause": r["cause"], "action": r["action"],
                        "expect": None if r["expect"] is None else json.loads(r["expect"]),
                        "code": p.get("code", 0), "speed": speed, "stages": stages, "response_tags": tags, "unobserved": p.get("unobserved", []),
                        "start_at": r["start_at"], "stop_requested_at": r["stop_requested_at"], "end_at": end_at})
        out += self._instrument_key(equip_id, from_, to)
        return sorted(out, key=lambda e: (e["start_at"], e["event_id"]))

    def _instrument_key(self, equip_id: str | None, from_: str | None, to: str | None) -> list[dict]:
        sim_now = self.storage.sim_now()
        if self.instrument is None or sim_now is None:
            return []
        # 수집 서버에 데이터가 있는 구간만: 가장 이른 원시 값(지워졌으면 가장 이른 1시간 집계 구간 시작)부터 sim_now 까지
        first = self.storage.query("SELECT MIN(m) FROM (SELECT MIN(ts) m FROM ts_raw UNION ALL SELECT MIN(bucket_start) FROM ts_1h)")[0][0]
        lo = max(_epoch(from_), _epoch(first or sim_now)) if from_ else _epoch(first or sim_now)
        hi = min(_epoch(to), _epoch(sim_now) + 1) if to else _epoch(sim_now) + 1
        out = []
        for ev in self.instrument.events(lo, hi):
            if equip_id and ev.equip_id != equip_id:
                continue
            mode, cause, action = im.LABEL[ev.kind]
            out.append({"event_id": ev.event_id, "equip_id": ev.equip_id, "mode": mode, "kind": im.KIND, "cause": cause,
                        "action": action, "expect": None, "code": im.CODE, "stages": [],
                        "response_tags": [{"equip_id": ev.equip_id, "tag_id": t, "stage": None, "start_at": _db(ev.start)}
                                          for t in ev.tag_ids],
                        "unobserved": [], "start_at": _db(ev.start), "stop_requested_at": None, "end_at": _db(ev.end)})
        return out
