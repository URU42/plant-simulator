"""공장 하나의 실행 문맥 (다중 공장 설계 §4.7).

구성안·실행 설정·DB·배치·SSE 브로드캐스터를 한 묶음으로 든다. 수집 서버는 공장 키마다 하나씩 만들고
라우트는 요청 경로의 키로 문맥을 골라 쓴다.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from collector.ingest import IngestResult, Rejection
from collector.jobs import MAX_WINDOWS_PER_ADVANCE, JobRunner
from collector.plants import PlantEntry
from collector.seed_master import seed_master
from collector.storage import Storage
from collector.stream import Broadcaster
from common.config_models import PlantConfig, RuntimeConfig, TagKey, load_plant, load_runtime
from common.message import Sample
from common.timefmt import from_db, to_db

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
    catch_up_task: asyncio.Task | None = field(default=None)
    simulator_scenarios: list[str] = field(default_factory=list)   # 통합 실행에서 시뮬레이터에만 준 시나리오 파일

    @classmethod
    def open(cls, entry: PlantEntry) -> "PlantContext":
        plant = load_plant(entry.plant)
        runtime = load_runtime(entry.runtime)
        Path(entry.db).parent.mkdir(parents=True, exist_ok=True) if entry.db != ":memory:" else None
        storage = Storage(entry.db)
        seed_master(storage, plant)
        if entry.scenario:
            from collector.seed_history import seed_history   # 시나리오 → 정비·고장 이력 (개발용)
            seed_history(storage, entry.scenario, plant)
        return cls(key=entry.key, plant=plant, runtime=runtime, storage=storage, jobs=JobRunner(storage, plant, runtime),
                   broadcaster=Broadcaster(), listing=equipment_listing(plant, runtime), tag_keys=storage.tag_keys())

    def close(self) -> None:
        if self.catch_up_task:
            self.catch_up_task.cancel()
        self.storage.close()

    # --- 수신 핵심 (HTTP 라우트와 LocalSender 가 함께 쓴다) -------------------------
    def receive(self, batch_id: str, source_id: str, samples: list[Sample], received_at: str,
                rejections: list[Rejection] = ()) -> IngestResult:
        """검증을 마친 샘플을 저장하고 배치를 전진시킨다. 같은 batch_id 는 저장하지 않고, 보관 기간 밖 행은 거절한다."""
        reasons = list(rejections)
        oldest = self.oldest_allowed()
        accepted: list[Sample] = []
        for i, s in enumerate(samples):
            if oldest is not None and to_db(s.ts) < oldest:
                reasons.append(Rejection(index=i, tag_id=s.tag_id, reason=f"ts {s.ts.isoformat()} is older than raw retention"))
            else:
                accepted.append(s)
        if self.storage.batch_seen(batch_id):
            return IngestResult(batch_id=batch_id, duplicate=True, accepted=len(accepted), stored=0,
                                rejected=len(reasons), reasons=reasons)
        rows = [(s.equip_id, s.tag_id, to_db(s.ts), s.value, s.quality) for s in accepted]
        stored = self.storage.store_batch(batch_id, source_id, received_at, rows)
        if accepted:
            if self.jobs.advance(self.storage.sim_now()) >= MAX_WINDOWS_PER_ADVANCE:
                self.start_catch_up()
            if self.broadcaster.has_subscribers():         # 구독자가 없으면 직렬화도 하지 않는다
                self.broadcaster.publish("samples", [s.model_dump(mode="json") for s in accepted])
        return IngestResult(batch_id=batch_id, duplicate=False, accepted=len(accepted), stored=stored,
                            rejected=len(reasons), reasons=reasons)

    def receive_local(self, batch_id: str, source_id: str, samples: list[Sample]) -> IngestResult:
        """같은 프로세스의 생성기가 보낸 묶음. 받은 시각은 지금."""
        return self.receive(batch_id, source_id, samples, to_db(datetime.now(timezone.utc)))

    def start_catch_up(self) -> None:
        """밀린 구간이 상한을 넘으면 요청을 붙잡지 않고 백그라운드에서 이어서 따라잡는다. 이벤트 루프 밖(테스트)에서는 그 자리에서 끝낸다."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            while self.jobs.advance(self.storage.sim_now()):
                pass
            return
        if not self.catching_up:
            self.catch_up_task = asyncio.create_task(self.catch_up())

    async def catch_up(self) -> None:
        while True:
            await asyncio.sleep(0)                      # 다른 요청이 끼어들 틈을 준다
            if self.jobs.advance(self.storage.sim_now()) == 0:
                return

    # --- 조회 도우미 ---------------------------------------------------------
    @property
    def catching_up(self) -> bool:
        return self.catch_up_task is not None and not self.catch_up_task.done()

    def health(self) -> dict:
        return {"key": self.key, "plant_id": self.plant.plant.plant_id, "plant_name": self.plant.plant.name,
                "last_received_at": self.storage.last_received_at(), "sim_now": self.storage.sim_now(),
                "catching_up": self.catching_up, "simulator_scenarios": self.simulator_scenarios}

    def oldest_allowed(self) -> str | None:
        """시뮬레이터 현재 시각 기준 보관 기간 밖의 행은 받지 않는다. 백필을 다시 돌려도 DB가 부풀지 않는다."""
        sim_now = self.storage.sim_now()
        if sim_now is None:
            return None
        return to_db(from_db(sim_now) - timedelta(days=self.runtime.raw_retention_days))

    def with_status(self, row: dict | None, key: TagKey) -> dict:
        """현재값 행에 status 를 붙인다. 행이 없거나 전송·태그 주기 중 긴 쪽의 3배를 넘게 안 들어오면 NoData."""
        if row is None:
            return {"equip_id": key[0], "tag_id": key[1], "ts": None, "value": None, "quality": None,
                    "received_at": None, "status": "NoData"}
        age = (from_db(self.storage.sim_now()) - from_db(row["ts"])).total_seconds()
        interval = max(self.runtime.send_interval_sec, self.plant.tag_interval_sec(key, self.runtime.sample_interval_sec))
        return row | {"status": "NoData" if age > NO_DATA_SEND_INTERVALS * interval else row["quality"]}


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
                              "limits": t.limits.model_dump(exclude_none=True) if t.limits else None} for t in e.tags],
                })
    return out
