"""중앙 수집 서버 (FastAPI): 공장마다 /{공장}/ingest, 조회 API, /stream, 디버깅 화면 (기획서 6.4, 다중 공장 설계 §4.7).

실행: python -m collector.app --plants config/plants.yaml --port 8000
라우트는 한 번만 정의하고 경로 앞의 {plant_key} 로 공장 문맥(PlantContext)을 고른다. 루트 / 는 공장 목록, /health 는 전체 상태.
통합 실행에서는 /{공장}/anomalies 로 고장 모드(가상 시나리오)를 주입·멈추고, 남은 손상을 수리하고, 정답지를 낸다. 정상 운전에는 이상이 없다.
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import math
import os
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator, Literal
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from collector.anomaly_book import Busy, NotApplicable
from collector.ingest import RawBatch, schema_supported, validate_samples
from collector.plant_context import PlantContext
from collector.plants import PlantEntry, PlantsConfig, load_plants
from collector.settings import Settings
from collector.stream import QUEUE_SIZE
from common.config_models import ConfigError
from simulator.fault import load_fault_modes
from common.sim_clock import ControlledClock, KST, align_down
from common.timefmt import from_db, parse_iso, to_db

STATIC_DIR = Path(__file__).with_name("static")     # 디버깅 화면
log = logging.getLogger("collector")


# --- 공장 문맥 선택 -----------------------------------------------------------
def context(plant_key: str, request: Request) -> PlantContext:
    ctx = request.app.state.plants.get(plant_key)
    if ctx is None:
        raise HTTPException(404, f"unknown plant key {plant_key}")
    return ctx


router = APIRouter(prefix="/{plant_key}")


# --- 수집 ---------------------------------------------------------------------
def require_ingest_token(request: Request) -> None:
    """본문을 읽기 전에 쓰기 권한을 확인한다. 통합 실행인데 토큰이 없으면 외부 수신을 아예 닫는다(fail-closed)."""
    settings = request.app.state.settings
    if settings.ingest_token:
        if not hmac.compare_digest(request.headers.get("X-Ingest-Token", ""), settings.ingest_token):
            raise HTTPException(403, "invalid or missing X-Ingest-Token")
    elif settings.with_simulators:
        raise HTTPException(403, "external writes are disabled: set INGEST_TOKEN to allow POST /ingest and /anomalies in embedded mode")


@router.post("/ingest")
async def post_ingest(request: Request, ctx: PlantContext = Depends(context)):
    require_ingest_token(request)                 # 본문을 읽기 전에 (FastAPI 의 본문 검증은 의존성보다 먼저 돌므로 직접 순서를 정한다)
    try:
        batch = RawBatch.model_validate(await request.json())
    except (ValueError, ValidationError) as e:    # JSON 오류도 ValueError
        raise HTTPException(422, f"invalid body: {str(e)[:300]}")
    if not schema_supported(batch.schema_version):
        raise HTTPException(400, f"unsupported schema_version {batch.schema_version}")
    accepted, rejected = validate_samples(batch.samples, ctx.tag_keys)
    return await asyncio.to_thread(ctx.receive, batch.batch_id, batch.source_id, accepted, to_db(datetime.now(timezone.utc)), rejected)


# --- 조회 ---------------------------------------------------------------------
def query_time(name: str, value: str) -> str:
    """조회 파라미터 시각을 저장 형식으로. 못 읽으면 400 (URL 의 '+' 는 공백이 되므로 Z 나 %2B 로 쓰라고 알린다)."""
    try:
        return to_db(parse_iso(value))
    except ValueError:
        raise HTTPException(400, f"invalid {name} {value!r}: use ISO 8601 with Z (2026-11-12T00:00:00Z) "
                                 "or an encoded offset (2026-11-12T09:00:00%2B09:00)")


@router.get("/")
async def plant_page(ctx: PlantContext = Depends(context)):
    # 디버깅 화면은 자주 바뀌므로 브라우저가 캐시하지 않게 한다
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html", headers={"Cache-Control": "no-cache"})


@router.get("/health")
def plant_health(ctx: PlantContext = Depends(context)):
    """조회 핸들러는 동기(def)라 스레드풀에서 돈다: 워커가 쥔 저장소 락을 이벤트 루프가 기다리지 않는다."""
    if ctx.simulator_error:
        raise HTTPException(503, {"status": "degraded", **ctx.health()})
    return {"status": "ok", **ctx.health()}


@router.get("/equipment")
def equipment(ctx: PlantContext = Depends(context)):
    """계층 순서의 설비 목록과 태그 (화면이 표를 그릴 때)."""
    return ctx.listing


@router.get("/flow")
def flow(ctx: PlantContext = Depends(context)):
    """공정 흐름 그래프 (가상 공장). 3D 뷰어가 하류 영향을 계산·표시할 때 읽는다. 축 연결은 equipment 의 driven_by."""
    f = ctx.plant.flow
    return {"plant": ctx.plant.plant.plant_id, "nodes": f.nodes,
            "equipment": [{"equip_id": e.equip_id, "driven_by": e.driven_by} for e in ctx.plant.equipment],
            "edges": [{"from": a, "to": b, "label": label, "stream": stream} for a, b, label, stream in f.edges]}


@router.get("/current")
def current_all(ctx: PlantContext = Depends(context)):
    """모든 태그의 현재값. 순서는 /equipment 와 같다."""
    rows = {(r["equip_id"], r["tag_id"]): r for r in ctx.storage.query_dicts("SELECT * FROM tag_current")}
    return [ctx.current_row(rows.get((e["equip_id"], t["tag_id"])), (e["equip_id"], t["tag_id"]))
            for e in ctx.listing for t in e["tags"]]


@router.get("/equipment/{equip_id}/tags/{tag_id}/current")
def tag_current(equip_id: str, tag_id: str, ctx: PlantContext = Depends(context)):
    key = (equip_id, tag_id)
    if key not in ctx.tag_keys:
        raise HTTPException(404, f"unknown tag {equip_id}/{tag_id}")
    return ctx.current_row(ctx.storage.current(key), key)


@router.get("/timeseries")
def timeseries(equip_id: str, tag_id: str, from_: str = Query(alias="from"), to: str = Query(), interval: str = "1h",
                     ctx: PlantContext = Depends(context)):
    if (equip_id, tag_id) not in ctx.tag_keys:
        raise HTTPException(404, f"unknown tag {equip_id}/{tag_id}")
    if interval not in ("10m", "1h"):
        raise HTTPException(400, "interval must be 10m or 1h")
    return ctx.storage.query_dicts(
        f"SELECT equip_id, tag_id, bucket_start AS ts, avg, min, max, sd, count FROM ts_{interval} "
        "WHERE equip_id = ? AND tag_id = ? AND bucket_start >= ? AND bucket_start < ? ORDER BY bucket_start",
        (equip_id, tag_id, query_time("from", from_), query_time("to", to)),
    )


# --- 고장 모드 사건: 주입·멈춤·정답지 ------------------------------------------
START_DELAY_SEC = 3     # 주입·멈춤은 시뮬레이터 시각 기준 이만큼 뒤부터. 장부가 이미 만든 구간 끝보다 앞서면 더 미룬다


class InjectBody(BaseModel):
    equip_id: str
    mode: str               # config/fault_modes.yaml 의 고장 모드 id
    speed: Literal[1, 3, 5, 10] = 1     # 진행 속도 (simulator.fault.SPEEDS)


def require_live(request: Request, ctx: PlantContext) -> None:
    """사건 쓰기(POST) 확인: 토큰 → 통합 실행(409) → 생성기 죽음(503) → 실시간(503). DB 는 읽지 않는다(이벤트 루프 위)."""
    require_ingest_token(request)
    if not request.app.state.settings.with_simulators:
        raise HTTPException(409, "embedded only")
    if ctx.simulator_error:
        raise HTTPException(503, f"simulator stopped: {ctx.simulator_error}")
    if not ctx.live:
        raise HTTPException(503, "backfilling")


def event_time(ctx: PlantContext) -> float:
    """요청 시각(epoch 초) = ceil(시뮬레이터 현재 시각) + 3. 저장소를 읽으므로 워커 스레드에서 부른다. sim_now 를 모르면 503."""
    sim_now = ctx.storage.sim_now()
    if sim_now is None:
        raise HTTPException(503, "backfilling")
    return math.ceil(from_db(sim_now).timestamp()) + START_DELAY_SEC


def _utc(epoch: float) -> str:
    return to_db(datetime.fromtimestamp(epoch, timezone.utc))


def _require_equipment(ctx: PlantContext, equip_id: str) -> None:
    if equip_id not in ctx.plant.equipment_by_id():
        raise HTTPException(404, f"unknown equipment {equip_id}")


@router.get("/fault-modes")
def fault_modes(ctx: PlantContext = Depends(context)):
    """이 공장에서 걸 수 있는 고장 모드와 대상 설비. 정답 분류(label)·기대값은 싣지 않는다 (정답지는 /anomalies)."""
    out = []
    for mode in ctx.book.modes.values():
        equip_ids = [e.equip_id for e in ctx.plant.equipment if mode in ctx.book.applicable(e.equip_id)]
        if equip_ids:
            out.append({"mode": mode.id, "name": mode.name, "equip_ids": equip_ids, "unobserved": mode.unobserved,
                        "stages": [{"id": s.id, "name": s.name, "at_sec": s.at_sec, "damage": s.damage} for s in mode.stages]})
    return out


@router.post("/anomalies", status_code=201)
async def inject_anomaly(request: Request, ctx: PlantContext = Depends(context)):
    """설비 하나에 고장 모드 하나를 건다. 멈출 때까지 간다. 그 설비에 안 멈춘 사건이 있으면 409,
    모르는 고장 모드이거나 그 설비에 걸 수 없으면 422. start_at 은 실제 시작 시각(ceil(sim_now)+3,
    생성기가 이미 그 뒤까지 만들었으면 그 끝)."""
    require_live(request, ctx)                    # 본문보다 먼저 (post_ingest 와 같은 이유)
    try:
        body = InjectBody.model_validate(await request.json())
    except (ValueError, ValidationError) as e:
        raise HTTPException(422, f"invalid body: {str(e)[:300]}")
    _require_equipment(ctx, body.equip_id)
    try:
        event = await asyncio.to_thread(lambda: ctx.book.inject(body.equip_id, body.mode, event_time(ctx), body.speed))
    except Busy:
        raise HTTPException(409, f"{body.equip_id} has a live fault: stop it first")
    except NotApplicable as e:
        raise HTTPException(422, str(e))
    return {"event_id": event.event_id, "equip_id": event.equip_id, "mode": event.mode.id, "speed": event.speed,
            "start_at": _utc(event.start)}


@router.post("/anomalies/stop")
async def stop_all_anomalies(request: Request, ctx: PlantContext = Depends(context)):
    """그 공장의 안 멈춘 사건을 모두 멈춘다. stop_requested_at 은 실제 멈춤 시각(주입의 start_at 과 같은 규칙)."""
    require_live(request, ctx)
    at, stopped = await asyncio.to_thread(lambda: ctx.book.stop(None, event_time(ctx)))
    return {"stopped": stopped, "stop_requested_at": _utc(at)}


@router.post("/anomalies/{equip_id}/stop")
async def stop_anomaly(equip_id: str, request: Request, ctx: PlantContext = Depends(context)):
    """그 설비의 안 멈춘 사건을 멈춘다. 모르는 설비이거나 멈출 사건이 없으면 404."""
    require_live(request, ctx)
    _require_equipment(ctx, equip_id)
    at, stopped = await asyncio.to_thread(lambda: ctx.book.stop(equip_id, event_time(ctx)))
    if not stopped:
        raise HTTPException(404, f"no live anomaly on {equip_id}")
    return {"stopped": stopped, "stop_requested_at": _utc(at)}


@router.post("/anomalies/{equip_id}/repair")
async def repair_anomaly(equip_id: str, request: Request, ctx: PlantContext = Depends(context)):
    """그 설비의 멈췄지만 손상이 남은 사건을 고친다 (손상 효과가 회복 시간 동안 0 이 된다). 모르는 설비이거나 고칠 손상이 없으면 404.
    안 멈춘 사건의 손상은 고치지 않는다 (먼저 멈춘다)."""
    require_live(request, ctx)
    _require_equipment(ctx, equip_id)
    at, repaired = await asyncio.to_thread(lambda: ctx.book.repair(equip_id, event_time(ctx)))
    if not repaired:
        raise HTTPException(404, f"no damage left to repair on {equip_id}")
    return {"repaired": repaired, "repair_requested_at": _utc(at)}


def _require_physics_evaluation_token(request: Request, ctx: PlantContext) -> None:
    if ctx.runtime.physics is None:
        return
    token = request.app.state.settings.ingest_token
    if not token or not hmac.compare_digest(request.headers.get("X-Ingest-Token", ""), token):
        raise HTTPException(403, "physics scenario records require the evaluation operator token")


@router.get("/anomalies/damaged")
def damaged_anomalies(request: Request, ctx: PlantContext = Depends(context)):
    """멈췄지만 손상이 남아 아직 안 고친 사건 (화면의 수리 버튼용). 정답 분류는 싣지 않는다."""
    _require_physics_evaluation_token(request, ctx)
    return [{"event_id": e.event_id, "equip_id": e.equip_id, "mode": e.mode.id, "start_at": _utc(e.start), "stop_requested_at": _utc(e.stop)}
            for e in ctx.book.damaged()]


@router.get("/anomalies/active")
def active_anomalies(request: Request, ctx: PlantContext = Depends(context)):
    """안 멈춘 고장 모드 사건: event_id·설비·모드·시작. 정답 분류는 싣지 않는다 (화면의 적용/해제 상태용)."""
    _require_physics_evaluation_token(request, ctx)
    return [{"event_id": e.event_id, "equip_id": e.equip_id, "mode": e.mode.id, "speed": e.speed, "start_at": _utc(e.start)}
            for e in ctx.book.active()]


@router.get("/anomalies")
def answer_key(request: Request, equip_id: str | None = None, from_: str | None = Query(default=None, alias="from"), to: str | None = None,
               ctx: PlantContext = Depends(context)):
    """정답지: 사건마다 고장 모드·분류·기대값·단계와 태그별 효과 시작·시작·멈춤·끝(멈춤 + 회복). from/to 는 구간과 겹치는 사건."""
    _require_physics_evaluation_token(request, ctx)
    return ctx.book.answer_key(equip_id, query_time("from", from_) if from_ else None, query_time("to", to) if to else None)


class SimulationAction(BaseModel):
    model_config = {"extra": "forbid"}
    command_id: str = Field(min_length=1, max_length=64)
    action: Literal["start", "pause", "resume", "speed", "normal", "inject", "stop", "repair"]
    rate: Literal[1, 5, 10, 30, 60] | None = None
    mode: str | None = Field(default=None, max_length=80)
    equip_id: str | None = Field(default=None, max_length=80)
    scenario_speed: Literal[1, 3, 5, 10] = 1


def _simulation_state(ctx: PlantContext) -> dict:
    clock = ctx.simulation_clock
    if clock is None:
        raise HTTPException(404, "simulation control disabled")
    if ctx.simulator_error:
        raise HTTPException(503, "simulator unavailable")
    return {"phase": clock.phase, "rate": clock.rate, "sim_time": _utc(clock.now().timestamp())}


@router.get("/simulation")
def simulation_state(ctx: PlantContext = Depends(context)):
    return _simulation_state(ctx)


@router.get("/simulation/observations")
def simulation_observations(request: Request, equip_id: str | None = None,
                            from_: str | None = Query(default=None, alias="from"), to: str | None = None,
                            ctx: PlantContext = Depends(context)):
    """관측 전용 원시 표본. 종료 시각은 현재 시뮬레이션 시각으로 제한한다."""
    require_ingest_token(request)
    _simulation_state(ctx)
    if equip_id is not None:
        _require_equipment(ctx, equip_id)
    now = from_db(_simulation_state(ctx)["sim_time"])
    end = min(from_db(query_time("to", to)), now) if to is not None else now
    start = from_db(query_time("from", from_)) if from_ is not None else end - timedelta(seconds=30)
    if not timedelta(0) <= end - start <= timedelta(minutes=5):
        raise HTTPException(422, "observation range must be within five minutes and end at or before simulation time")
    condition = " AND equip_id = ?" if equip_id is not None else ""
    params = (to_db(start), to_db(end)) if equip_id is None else (to_db(start), to_db(end), equip_id)
    return ctx.storage.query_dicts(
        "SELECT equip_id, tag_id, ts, value, received_at FROM ts_raw "
        "WHERE ts >= ? AND ts <= ? AND value IS NOT NULL" + condition + " ORDER BY ts, equip_id, tag_id", params,
    )


@router.post("/simulation/control")
async def simulation_control(body: SimulationAction, request: Request, ctx: PlantContext = Depends(context)):
    require_ingest_token(request)
    _simulation_state(ctx)
    clock = ctx.simulation_clock
    assert clock is not None
    async with ctx.control_lock:
        previous = ctx.control_receipts.get(body.command_id)
        if previous is not None:
            payload, state = previous
            if payload != body:
                raise HTTPException(409, "command_id already used for another command")
            return state
        if body.action in ("start", "resume"):
            clock.update(phase="running")
        elif body.action == "pause":
            if clock.phase == "ready":
                raise HTTPException(409, "simulation has not started")
            clock.update(phase="paused")
        elif body.action == "speed":
            if body.rate is None:
                raise HTTPException(422, "rate required")
            clock.update(rate=body.rate)
        elif body.action == "normal":
            at = clock.now().timestamp() + START_DELAY_SEC

            def restore():
                ctx.book.stop(None, at)
                for equip_id in {event.equip_id for event in ctx.book.damaged()}:
                    ctx.book.repair(equip_id, at)

            await asyncio.to_thread(restore)
        elif body.action in ("stop", "repair"):
            if not body.equip_id:
                raise HTTPException(422, "equipment required")
            _require_equipment(ctx, body.equip_id)
            operation = ctx.book.stop if body.action == "stop" else ctx.book.repair
            await asyncio.to_thread(operation, body.equip_id, clock.now().timestamp() + START_DELAY_SEC)
        elif body.action == "inject":
            if clock.phase != "running":
                raise HTTPException(409, "simulation must be running")
            if not body.equip_id or not body.mode:
                raise HTTPException(422, "equipment and mode required")
            _require_equipment(ctx, body.equip_id)
            try:
                await asyncio.to_thread(ctx.book.inject, body.equip_id, body.mode,
                                        clock.now().timestamp() + START_DELAY_SEC, body.scenario_speed)
            except Busy as exc:
                raise HTTPException(409, "equipment already has an active scenario") from exc
            except NotApplicable as exc:
                raise HTTPException(422, str(exc)) from exc
        state = _simulation_state(ctx)
        ctx.control_receipts[body.command_id] = (body, state)
        if len(ctx.control_receipts) > 1024:
            ctx.control_receipts.pop(next(iter(ctx.control_receipts)))
        return state


@router.get("/stream")
async def stream(ctx: PlantContext = Depends(context)):
    return StreamingResponse(ctx.broadcaster.subscribe(heartbeat=10), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# --- 앱 ----------------------------------------------------------------------
def create_app(plants: PlantsConfig, settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        modes = load_fault_modes(plants.fault_modes)
        app.state.plants = {entry.key: PlantContext.open(entry, modes, settings.redis_url, settings.reset_db_on_schema_change,
                                                         settings.profile)
                            for entry in plants.plants}
        app.state.started_at = to_db(datetime.now(timezone.utc))
        tasks: list[asyncio.Task] = []
        switch_interval = sys.getswitchinterval()
        if settings.with_simulators:
            from collector.embedded import EMBEDDED_CONCURRENCY, EMBEDDED_SWITCH_INTERVAL_SEC, run_plant_simulator
            sys.setswitchinterval(EMBEDDED_SWITCH_INTERVAL_SEC)
            limiter = asyncio.Semaphore(EMBEDDED_CONCURRENCY)
            for entry in plants.plants:
                ctx = app.state.plants[entry.key]
                if settings.simulation_control and entry.key == "cdu" and ctx.runtime.physics is not None:
                    stored = ctx.storage.sim_now()
                    anchor = align_down(datetime.now(KST), ctx.runtime.send_interval_sec)
                    if stored:
                        anchor = max(anchor, from_db(stored).astimezone(KST) + timedelta(seconds=ctx.runtime.sample_interval_sec))
                    ctx.simulation_clock = ControlledClock(anchor)
                task = asyncio.create_task(run_plant_simulator(ctx, entry, settings, limiter), name=f"sim-{entry.key}")
                task.add_done_callback(lambda t, ctx=ctx: _simulator_done(t, ctx))
                tasks.append(task)
        yield
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)   # 워커 스레드의 마지막 묶음이 끝날 때까지 기다린 뒤 닫는다
        for ctx in app.state.plants.values():
            ctx.close()
        sys.setswitchinterval(switch_interval)

    app = FastAPI(title="Skadi 앞단 수집 서버", lifespan=lifespan, root_path=settings.root_path)
    app.state.plants = {}
    app.state.settings = settings
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["GET", "HEAD", "OPTIONS"],
                           allow_headers=["*"])

    @app.get("/health")
    def root_health(request: Request):
        """전체 상태. 통합 실행의 생성기가 하나라도 죽었으면 503 → liveness 가 파드를 다시 띄운다."""
        plants_health = {key: ctx.health() for key, ctx in request.app.state.plants.items()}
        body = {"status": "ok", "started_at": request.app.state.started_at, "plants": plants_health}
        if any(c.simulator_error for c in request.app.state.plants.values()):
            raise HTTPException(503, body | {"status": "degraded"})
        return body

    @app.get("/stream")
    async def root_stream(request: Request):
        """모든 공장의 초 단위 메시지를 연결 하나로 (이벤트 이름 = 공장 키, 데이터 = Redis Stream 과 같은 {plant, plant_id, ts, samples}).
        /{plant_key} 보다 먼저 등록해야 'stream' 이 공장 키로 잡히지 않는다."""
        return StreamingResponse(merged_stream(request.app.state.plants), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    @app.get("/", response_class=HTMLResponse)
    async def root_index(request: Request):
        return _plants_page(request.app.state.plants, request.scope.get("root_path", ""), request.app.state.settings.with_simulators)

    @app.get("/{plant_key}")
    async def plant_root(plant_key: str, request: Request):
        context(plant_key, request)                 # 모르는 키면 404
        return RedirectResponse(f"{request.scope.get('root_path', '')}/{plant_key}/", status_code=307)

    app.include_router(router)
    return app


def _simulator_done(task: asyncio.Task, ctx: PlantContext) -> None:
    """통합 실행의 공장 태스크가 예외로 끝나면 기록만 한다. /health 가 503 이 되어 k8s liveness 가 파드를 다시 띄우고,
    재개 로직이 마지막 시각부터 이어 채운다. (프로세스를 직접 죽이지 않는다 — 테스트 러너까지 끝내 버린다.)"""
    if task.cancelled() or task.exception() is None:
        return
    ctx.simulator_error = repr(task.exception())
    log.error("embedded simulator %s died: %s", task.get_name(), ctx.simulator_error)


def check_plants(plants: PlantsConfig, reset_db: bool = False, profile: str = "demo") -> None:
    """서버를 띄우기 전에 공장을 하나씩 열어 본다. 설정·DB 문제는 traceback 대신 "plant <key>: 이유" 한 줄로 알린다.
    reset_db 면 스키마 버전이 다른 DB 파일은 여기서 지우고 새로 만든다."""
    try:
        modes = load_fault_modes(plants.fault_modes)
    except (OSError, ValueError) as e:       # 파일 없음·형식 오류 (pydantic 검증 오류도 ValueError)
        raise ConfigError(f"fault modes {plants.fault_modes}: {e}") from None
    for entry in plants.plants:
        try:
            PlantContext.open(entry, modes, reset_db=reset_db, profile=profile).close()
        except ConfigError as e:
            raise ConfigError(f"plant {entry.key}: {e}") from None


def create_single_app(plant_path, runtime_path, db_path, key: str = "plant", settings: Settings | None = None) -> FastAPI:
    """공장 하나짜리 서버 (테스트·호환용). 경로는 /{key}/... 다."""
    entry = PlantEntry(key=key, plant=str(plant_path), runtime=str(runtime_path), db=str(db_path))
    return create_app(PlantsConfig(plants=[entry]), settings)


async def merged_stream(plants: dict[str, PlantContext]) -> AsyncIterator[str]:
    """공장별 초 단위 메시지(data_broadcaster, 이벤트 이름 = 공장 키)를 한 줄기로 합친다. Redis Stream 으로 보내는 메시지와 같다.
    공장마다 연결을 열면 HTTP/1.1 의 호스트당 연결 한도(6)를 거의 다 쓴다. 느린 구독자는 공장 브로드캐스터 쪽에서 오래된 이벤트부터 버린다."""
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=QUEUE_SIZE)

    async def pump(key: str, ctx: PlantContext) -> None:
        async for message in ctx.data_broadcaster.subscribe():
            await q.put(message)

    tasks = [asyncio.create_task(pump(k, c)) for k, c in plants.items()]
    try:
        while True:
            yield await q.get()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)     # 공장별 구독이 풀린 뒤에 끝낸다


def _plants_page(plants: dict[str, PlantContext], root: str = "", embedded: bool = False) -> str:
    rows = "".join(
        f"<tr><td><a href='{root}/{escape(k)}/'>/{escape(k)}/</a></td><td>{escape(c.plant.plant.plant_id)}</td>"
        f"<td>{escape(c.plant.plant.name)}</td><td class='num'>{len(c.plant.equipment)}</td>"
        f"<td class='num'>{len(c.plant.tag_keys())}</td><td><a href='{root}/{escape(k)}/health'>health</a></td></tr>"
        for k, c in plants.items())
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>plant-simulator · 공장 목록</title>
<style>body{{font:14px/1.4 -apple-system,"Apple SD Gothic Neo",sans-serif;margin:24px}}table{{border-collapse:collapse}}
th,td{{padding:4px 12px;border-bottom:1px solid #e3e3e3;text-align:left}}td.num{{text-align:right}}</style></head>
<body><h1>plant-simulator · 공장 목록</h1>
<table><thead><tr><th>경로</th><th>plant_id</th><th>이름</th><th>설비</th><th>태그</th><th></th></tr></thead><tbody>{rows}</tbody></table>
<p><a href="{root}/health">전체 상태 (/health)</a></p>{_fault_panel(plants, root, embedded)}{_snippet("stream_log.html", plants, root)}</body></html>"""


def _fault_panel(plants: dict[str, PlantContext], root: str, embedded: bool) -> str:
    """공장 목록 아래의 고장 설정 (설비 - 고장 유형 - 적용/해제). 주입은 통합 실행에서만 된다."""
    if not embedded:
        return "<h2>고장 설정</h2><p>통합 실행(WITH_SIMULATORS=1)에서만 고장 모드를 걸 수 있습니다.</p>"
    return _snippet("fault_panel.html", plants, root)


def _snippet(name: str, plants: dict[str, PlantContext], root: str) -> str:
    """static/ 의 화면 조각에 공장 목록 데이터(__PAGE_DATA__)를 채운다."""
    data = {"root": root, "plants": [{"key": k, "plant_id": c.plant.plant.plant_id, "name": c.plant.plant.name} for k, c in plants.items()]}
    return (STATIC_DIR / name).read_text(encoding="utf-8").replace(
        "__PAGE_DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))


# --- 실행 진입점 -----------------------------------------------------------
def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Skadi 앞단 수집 서버 (여러 공장)")
    p.add_argument("--plants", default=os.environ.get("PLANTS_CONFIG", "config/plants.yaml"), help="공장 목록 yaml")
    p.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    p.add_argument("--with-simulators", action="store_true", default=None,
                   help="공장 목록의 생성기를 같은 프로세스에서 돌린다 (WITH_SIMULATORS=1 과 같다)")
    p.add_argument("--time-mode", choices=["backfill", "live", "accelerated"], help="통합 실행에서 각 공장의 time_mode 를 덮어쓴다")
    p.add_argument("--backfill-days", type=int, help="통합 실행에서 각 공장의 backfill_days 를 덮어쓴다")
    p.add_argument("--profile", choices=["demo", "eval"], help="실행 용도 (DATA_PROFILE 과 같다). eval = 정상 억제 표집 끔·계측 이상 켬")
    return p.parse_args(argv)


def main(argv=None) -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv)
    plants = load_plants(args.plants)
    try:
        settings = Settings.from_env(with_simulators=args.with_simulators or None, time_mode=args.time_mode,
                                     backfill_days=args.backfill_days, profile=args.profile)
        check_plants(plants, settings.reset_db_on_schema_change, settings.profile)
    except ConfigError as e:
        sys.exit(f"error: {e}")
    uvicorn.run(create_app(plants, settings), host=args.host, port=args.port, root_path=settings.root_path)


if __name__ == "__main__":
    main()
