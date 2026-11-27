"""공장 하나의 실행 문맥 (다중 공장 설계 §4.7).

구성안·실행 설정·DB·배치·SSE 브로드캐스터를 한 묶음으로 든다. 수집 서버는 공장 키마다 하나씩 만들고
라우트는 요청 경로의 키로 문맥을 골라 쓴다.
"""
from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from collector.anomaly_book import AnomalyBook
from collector.data_stream import DataStream
from collector.ingest import IngestResult, Rejection
from collector.jobs import MAX_WINDOWS_PER_ADVANCE, JobRunner
from collector.plants import PlantEntry
from collector.seed_master import seed_master
from collector.storage import Storage
from collector.stream import Broadcaster
from common.config_models import ConfigError, PlantConfig, RuntimeConfig, TagKey, apply_profile, load_plant, load_runtime
from common.message import Sample
from common.sim_clock import ControlledClock
from simulator.fault import FaultMode
from simulator.physics_adapter import PhysicsAdapter, PhysicsBundle, load_physics
from simulator.instrument import InstrumentSchedule
from common.timefmt import from_db, to_db




@dataclass
class PlantContext:
    key: str
    plant: PlantConfig
    runtime: RuntimeConfig
    storage: Storage
    jobs: JobRunner
    broadcaster: Broadcaster                  # /{공장}/stream: 받은 묶음의 표본 배열 (현재값 화면)
    listing: list[dict]                       # GET /equipment 응답 (계층 순서)
    tag_keys: set[TagKey]
    book: AnomalyBook                         # 고장 모드 사건 장부 (통합 실행 생성기의 EventSource, 정답지)
    live: bool = False                        # 생성기가 실시간(또는 가속)으로 시계를 따라잡았는가. 백필·밀린 구간 재생 중이면 False
    catch_up_task: asyncio.Task | None = field(default=None)
    simulation_clock: ControlledClock | None = None
    control_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    control_receipts: dict = field(default_factory=dict)
    simulator_error: str | None = field(default=None)               # 통합 실행의 생성기 태스크가 예외로 끝났을 때
    # 저장·배치 전진은 워커 스레드에서도 돌 수 있다(통합 실행). 한 공장의 SQLite 트랜잭션이 겹치지 않도록 락으로 직렬화한다.
    lock: threading.Lock = field(default_factory=threading.Lock)
    loop: asyncio.AbstractEventLoop | None = field(default=None)    # SSE 발행을 루프 스레드로 넘기기 위해
    # 초마다 메시지 하나 {plant, plant_id, ts, samples}: 루트 /stream(브라우저 로그)과 Redis Stream 이 같은 메시지를 받는다
    data_broadcaster: Broadcaster = field(default_factory=Broadcaster)
    data_stream: DataStream | None = field(default=None)

    @classmethod
    def open(cls, entry: PlantEntry, modes: dict[str, FaultMode], redis_url: str | None = None,
             reset_db: bool = False, profile: str = "demo", simulation_control: bool = False) -> "PlantContext":
        """modes 는 공장들이 함께 쓰는 고장 모드 정의(config/fault_modes.yaml). redis_url 이 있으면 Redis Stream 으로도 보낸다.
        조작 시뮬레이션(simulation_control 이고 트윈 실행 설정인 공장)은 보내지 않는다: 일부러 낸 고장이 AE·업무 알람으로 이어지지 않게.
        reset_db 면 스키마 버전이 다른 DB 파일을 지우고 새로 만든다. profile 은 실행 용도(demo·eval)로 DB 마다 하나로 고정된다."""
        plant = load_plant(entry.plant)
        runtime = apply_profile(load_runtime(entry.runtime), profile)
        physics = load_physics(plant, runtime.physics, runtime.physics_general) if runtime.physics is not None else None
        Path(entry.db).parent.mkdir(parents=True, exist_ok=True) if entry.db != ":memory:" else None
        storage = Storage(entry.db, reset_on_schema_change=reset_db)
        _pin_profile(storage, entry.db, profile)
        seed_master(storage, plant)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        return cls(key=entry.key, plant=plant, runtime=runtime, storage=storage, jobs=JobRunner(storage, plant, runtime),
                   broadcaster=Broadcaster(), listing=equipment_listing(plant, runtime, physics), tag_keys=storage.tag_keys(),
                   loop=loop, book=AnomalyBook(storage, plant, modes, _instrument(plant, runtime), physics),
                   data_stream=DataStream.from_url(redis_url, entry.key) if redis_url and not (simulation_control and runtime.twin) else None)

    def set_live(self, at: float) -> None:
        """생성기가 실시간으로 넘어가 시계를 따라잡았다(at = 따라잡은 첫 구간 시작, epoch 초). 이때부터 주입을 받는다."""
        self.live = True

    def close(self) -> None:
        if self.catch_up_task:
            self.catch_up_task.cancel()
        if self.data_stream:
            self.data_stream.close()
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
        rows = [(s.equip_id, s.tag_id, to_db(s.ts), s.value) for s in accepted]
        with self.lock:
            if self.storage.batch_seen(batch_id):
                return IngestResult(batch_id=batch_id, duplicate=True, accepted=len(accepted), stored=0,
                                    rejected=len(reasons), reasons=reasons)
            stored = self.storage.store_batch(batch_id, source_id, received_at, rows)
            behind = self.jobs.advance(self.storage.sim_now()) >= MAX_WINDOWS_PER_ADVANCE if accepted else False
        if behind:
            self.start_catch_up()
        if accepted and (self.broadcaster.has_subscribers() or self.data_broadcaster.has_subscribers() or self.data_stream):
            dumped = [s.model_dump(mode="json") for s in accepted]       # 받는 곳이 없으면 직렬화도 하지 않는다
            messages = self.messages(accepted, dumped)
            if self.data_stream:
                self.data_stream.submit(messages)
            self._publish("samples", dumped, self.broadcaster)
            for m in messages:
                self._publish(self.key, m, self.data_broadcaster)
        return IngestResult(batch_id=batch_id, duplicate=False, accepted=len(accepted), stored=stored,
                            rejected=len(reasons), reasons=reasons)

    def _on_loop_thread(self) -> bool:
        """지금 이 공장의 이벤트 루프 스레드 위에 있는가 (워커 스레드면 False)."""
        try:
            return asyncio.get_running_loop() is self.loop
        except RuntimeError:
            return False

    def messages(self, samples: list[Sample], dumped: list[dict]) -> list[dict]:
        """측정 시각(초)마다 메시지 하나 {plant, plant_id, ts, samples}, 시각 순. 10초 태그는 그 초의 메시지에만 들어 있다."""
        by_ts: dict = {}
        for s, d in zip(samples, dumped):
            by_ts.setdefault(s.ts, []).append(d)
        return [{"plant": self.key, "plant_id": self.plant.plant.plant_id, "ts": rows[0]["ts"], "samples": rows}
                for _, rows in sorted(by_ts.items())]

    def _publish(self, event: str, data, broadcaster: Broadcaster) -> None:
        """SSE 큐는 루프 스레드에서만 만진다. 워커 스레드에서 왔으면 루프로 넘긴다."""
        if not broadcaster.has_subscribers():
            return
        if self.loop is not None and self.loop.is_running() and not self._on_loop_thread():
            self.loop.call_soon_threadsafe(broadcaster.publish, event, data)
        else:
            broadcaster.publish(event, data)

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
                "catching_up": self.catching_up, "live": self.live, "simulator_error": self.simulator_error}

    def oldest_allowed(self) -> str | None:
        """시뮬레이터 현재 시각 기준 보관 기간 밖의 행은 받지 않는다. 백필을 다시 돌려도 DB가 부풀지 않는다."""
        sim_now = self.storage.sim_now()
        if sim_now is None:
            return None
        return to_db(from_db(sim_now) - timedelta(days=self.runtime.raw_retention_days))

    @staticmethod
    def current_row(row: dict | None, key: TagKey) -> dict:
        """현재값 행. 아직 값이 없는 태그는 ts·value 가 null 이다."""
        return row or {"equip_id": key[0], "tag_id": key[1], "ts": None, "value": None, "received_at": None}


def equipment_listing(plant: PlantConfig, runtime: RuntimeConfig, physics: PhysicsAdapter | PhysicsBundle | None = None) -> list[dict]:
    """unit_groups → units → 설비(yaml 순서) 순으로 설비와 태그를 펼친다. 마스터 참고 필드와 한계값도 함께.
    control 은 운전점을 반영한 μ·σ (탐지 쪽 Nelson 기준)."""
    control = plant.control_by_key()
    out = []
    for group in plant.plant.unit_groups:
        for unit in group.units:
            for e in plant.equipment:
                if e.unit_id != unit.id:
                    continue
                out.append({
                    "equip_id": e.equip_id, "name": e.name, "type": e.type, "unit_id": unit.id, "unit_name": unit.name,
                    "unit_group_id": group.id, "unit_group_name": group.name, "driven_by": e.driven_by,
                    "operating": e.operating.model_dump() if e.operating else None,
                    "tags": [{"tag_id": t.tag_id, "signal": t.signal, "unit": t.unit, "description": t.description,
                              "interval_sec": plant.tag_interval_sec((e.equip_id, t.tag_id), runtime.sample_interval_sec),
                              "decimals": t.decimals, "normal_ref": t.normal_ref, "limit_basis": t.limit_basis,
                              **({"measurement_model": ("physical" if (e.equip_id, t.tag_id) in physics.tag_keys else "empirical"),
                                  "hydraulic_role": physics.roles.get(e.equip_id), "circuit_id": physics.circuit_of(e.equip_id)}
                                 if physics is not None else {}),
                              "limits": t.limits.model_dump(exclude_none=True) if t.limits else None,
                              "control": dict(zip(("mean", "sigma"), control[(e.equip_id, t.tag_id)]))} for t in e.tags],
                })
    return out


def _instrument(plant: PlantConfig, runtime: RuntimeConfig) -> InstrumentSchedule | None:
    """정답지에 실을 계측 이상 일정. 생성기와 같은 실행 설정(시드·빈도)으로 다시 계산한다."""
    cfg = runtime.instrument_faults
    return InstrumentSchedule(plant, cfg, runtime.seed, runtime.sample_interval_sec) if cfg.enabled else None


def _pin_profile(storage: Storage, db: str, profile: str) -> None:
    """DB 의 실행 용도를 기록하고, 다른 용도로 만든 DB 면 멈춘다. 기록 전에 데이터가 있던 DB 는 demo 로 본다(이 기능 전 실행)."""
    rows = storage.query("SELECT value FROM run_meta WHERE name = 'profile'")
    found = rows[0][0] if rows else ("demo" if storage.sim_now() is not None else None)
    if found is not None and found != profile:
        storage.close()
        raise ConfigError(f"DB {db} was generated with profile {found!r}, not {profile!r}: the answer key would mix both. "
                          "Point this plant's db to another file for this profile, or move or delete the file")
    if not rows:
        storage.execute("INSERT INTO run_meta (name, value) VALUES ('profile', ?)", (profile,))
