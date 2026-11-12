"""공장 하나의 실행 문맥 (다중 공장 설계 §4.7).

구성안·실행 설정·DB·배치·SSE 브로드캐스터를 한 묶음으로 든다. 수집 서버는 공장 키마다 하나씩 만들고
라우트는 요청 경로의 키로 문맥을 골라 쓴다.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from collector.alarms import AlarmTracker
from collector.ingest import IngestResult, Rejection
from collector.jobs import MAX_WINDOWS_PER_ADVANCE, JobRunner
from collector.plants import PlantEntry
from collector.seed_master import seed_master
from collector.storage import Storage
from collector.stream import Broadcaster
from common.config_models import PlantConfig, RuntimeConfig, TagKey, load_plant, load_runtime
from common.message import Sample
from common.timefmt import from_db, to_db

log = logging.getLogger("collector")

NO_DATA_SEND_INTERVALS = 3      # 전송 주기(또는 더 긴 태그 주기) 3번 동안 안 들어오면 NoData (기획서 6.2)


@dataclass
class PlantContext:
    key: str
    plant: PlantConfig
    runtime: RuntimeConfig
    storage: Storage
    jobs: JobRunner
    broadcaster: Broadcaster
    listing: list[dict]                       # GET /equipment 응답 (계층 순서)
    tag_keys: set[TagKey]
    alarms: AlarmTracker                      # 받을 때마다 Nelson 판정해 nelson_alarm 에 적는다
    catch_up_task: asyncio.Task | None = field(default=None)
    simulator_error: str | None = field(default=None)               # 통합 실행의 생성기 태스크가 예외로 끝났을 때
    # 저장·배치 전진은 워커 스레드에서도 돌 수 있다(통합 실행). 한 공장의 SQLite 트랜잭션이 겹치지 않도록 락으로 직렬화한다.
    lock: threading.Lock = field(default_factory=threading.Lock)
    loop: asyncio.AbstractEventLoop | None = field(default=None)    # SSE 발행을 루프 스레드로 넘기기 위해

    @classmethod
    def open(cls, entry: PlantEntry) -> "PlantContext":
        plant = load_plant(entry.plant)
        runtime = load_runtime(entry.runtime)
        Path(entry.db).parent.mkdir(parents=True, exist_ok=True) if entry.db != ":memory:" else None
        storage = Storage(entry.db)
        seed_master(storage, plant)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        return cls(key=entry.key, plant=plant, runtime=runtime, storage=storage, jobs=JobRunner(storage, plant, runtime),
                   broadcaster=Broadcaster(), listing=equipment_listing(plant, runtime), tag_keys=storage.tag_keys(),
                   alarms=AlarmTracker(storage, plant), loop=loop)

    def close(self) -> None:
        if self.catch_up_task:
            self.catch_up_task.cancel()
        self.storage.close()

    # --- 수신 핵심 (HTTP 라우트와 LocalSender 가 함께 쓴다) -------------------------
    def receive(self, batch_id: str, source_id: str, samples: list[Sample], received_at: str,
                rejections: list[Rejection] = ()) -> IngestResult:
        """검증을 마친 샘플을 저장하고 Nelson 판정을 반영한 뒤 배치를 전진시킨다. 같은 batch_id 는 저장하지 않고, 보관 기간 밖 행은 거절한다."""
        reasons = list(rejections)
        oldest = self.oldest_allowed()
        accepted: list[Sample] = []
        for i, s in enumerate(samples):
            if oldest is not None and to_db(s.ts) < oldest:
                reasons.append(Rejection(index=i, tag_id=s.tag_id, reason=f"ts {s.ts.isoformat()} is older than raw retention"))
            else:
                accepted.append(s)
        rows = [(s.equip_id, s.tag_id, to_db(s.ts), s.value, s.status) for s in accepted]
        with self.lock:
            if self.storage.batch_seen(batch_id):
                return IngestResult(batch_id=batch_id, duplicate=True, accepted=len(accepted), stored=0,
                                    rejected=len(reasons), reasons=reasons)
            stored = self.storage.store_batch(batch_id, source_id, received_at, rows)
            mismatched = self.alarms.observe(rows)
            behind = self.jobs.advance(self.storage.sim_now()) >= MAX_WINDOWS_PER_ADVANCE if accepted else False
        if mismatched:      # /alarms 는 수집 서버 판정을 따른다
            log.warning("plant %s: status mismatch on %d samples", self.key, mismatched)
        if behind:
            self.start_catch_up()
        if accepted and self.broadcaster.has_subscribers():   # 구독자가 없으면 직렬화도 하지 않는다
            self._publish([s.model_dump(mode="json") for s in accepted])
        return IngestResult(batch_id=batch_id, duplicate=False, accepted=len(accepted), stored=stored,
                            rejected=len(reasons), reasons=reasons)

    def _on_loop_thread(self) -> bool:
        """지금 이 공장의 이벤트 루프 스레드 위에 있는가 (워커 스레드면 False)."""
        try:
            return asyncio.get_running_loop() is self.loop
        except RuntimeError:
            return False

    def _publish(self, data: list[dict]) -> None:
        """SSE 큐는 루프 스레드에서만 만진다. 워커 스레드에서 왔으면 루프로 넘긴다."""
        if self.loop is not None and self.loop.is_running() and not self._on_loop_thread():
            self.loop.call_soon_threadsafe(self.broadcaster.publish, "samples", data)
        else:
            self.broadcaster.publish("samples", data)

    def _advance_locked(self) -> int:
        with self.lock:
            return self.jobs.advance(self.storage.sim_now())

    def receive_local(self, batch_id: str, source_id: str, samples: list[Sample]) -> IngestResult:
        """같은 프로세스의 생성기가 보낸 묶음. 받은 시각은 지금."""
        return self.receive(batch_id, source_id, samples, to_db(datetime.now(timezone.utc)))

    def start_catch_up(self) -> None:
        """밀린 구간이 상한을 넘으면 요청을 붙잡지 않고 백그라운드에서 이어서 따라잡는다. 이벤트 루프가 없으면(테스트) 그 자리에서 끝낸다."""
        loop = self.loop if self.loop is not None and self.loop.is_running() else None
        if loop is None:
            while self._advance_locked():
                pass
            return
        if self._on_loop_thread():
            if not self.catching_up:
                self.catch_up_task = asyncio.create_task(self.catch_up())
        else:
            loop.call_soon_threadsafe(self.start_catch_up)

    async def catch_up(self) -> None:
        while await asyncio.to_thread(self._advance_locked):
            pass

    # --- 조회 도우미 ---------------------------------------------------------
    @property
    def catching_up(self) -> bool:
        return self.catch_up_task is not None and not self.catch_up_task.done()

    def health(self) -> dict:
        return {"key": self.key, "plant_id": self.plant.plant.plant_id, "plant_name": self.plant.plant.name,
                "last_received_at": self.storage.last_received_at(), "sim_now": self.storage.sim_now(),
                "catching_up": self.catching_up, "simulator_error": self.simulator_error}

    def oldest_allowed(self) -> str | None:
        """시뮬레이터 현재 시각 기준 보관 기간 밖의 행은 받지 않는다. 백필을 다시 돌려도 DB가 부풀지 않는다."""
        sim_now = self.storage.sim_now()
        if sim_now is None:
            return None
        return to_db(from_db(sim_now) - timedelta(days=self.runtime.raw_retention_days))

    def with_status(self, row: dict | None, key: TagKey) -> dict:
        """현재값 행의 status 를 정한다. 행의 Good·Bad 를 그대로 쓰되, 행이 없거나 전송·태그 주기 중 긴 쪽의 3배를
        넘게 안 들어오면 NoData."""
        if row is None:
            return {"equip_id": key[0], "tag_id": key[1], "ts": None, "value": None,
                    "received_at": None, "status": "NoData"}
        age = (from_db(self.storage.sim_now()) - from_db(row["ts"])).total_seconds()
        interval = max(self.runtime.send_interval_sec, self.plant.tag_interval_sec(key, self.runtime.sample_interval_sec))
        return row | {"status": "NoData" if age > NO_DATA_SEND_INTERVALS * interval else row["status"]}


def equipment_listing(plant: PlantConfig, runtime: RuntimeConfig) -> list[dict]:
    """unit_groups → units → 설비(yaml 순서) 순으로 설비와 태그를 펼친다. 마스터 참고 필드와 한계값도 함께."""
    out = []
    for group in plant.plant.unit_groups:
        for unit in group.units:
            for e in plant.equipment:
                if e.unit_id != unit.id:
                    continue
                out.append({
                    "equip_id": e.equip_id, "name": e.name, "type": e.type, "unit_id": unit.id, "unit_name": unit.name,
                    "unit_group_id": group.id, "unit_group_name": group.name, "driven_by": e.driven_by,
                    "tags": [{"tag_id": t.tag_id, "signal": t.signal, "unit": t.unit, "description": t.description,
                              "interval_sec": plant.tag_interval_sec((e.equip_id, t.tag_id), runtime.sample_interval_sec),
                              "decimals": t.decimals, "normal_ref": t.normal_ref, "limit_basis": t.limit_basis,
                              "limits": t.limits.model_dump(exclude_none=True) if t.limits else None,
                              "control": t.control.model_dump()} for t in e.tags],
                })
    return out
