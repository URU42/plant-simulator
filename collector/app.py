"""중앙 수집 서버 (FastAPI): 공장마다 /{공장}/ingest, 조회 API, /stream, 디버깅 화면 (기획서 6.4, 다중 공장 설계 §4.7).

실행: python -m collector.app --plants config/plants.yaml --port 8000
라우트는 한 번만 정의하고 경로 앞의 {plant_key} 로 공장 문맥(PlantContext)을 고른다. 루트 / 는 공장 목록, /health 는 전체 상태.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse

from collector.ingest import RawBatch, schema_supported, validate_samples
from collector.plant_context import PlantContext
from collector.plants import PlantEntry, PlantsConfig, load_plants
from collector.settings import Settings
from common.config_models import ConfigError
from common.timefmt import parse_iso, to_db

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
@router.post("/ingest")
async def post_ingest(batch: RawBatch, request: Request, ctx: PlantContext = Depends(context)):
    token = request.app.state.settings.ingest_token
    if token and request.headers.get("X-Ingest-Token") != token:
        raise HTTPException(403, "invalid or missing X-Ingest-Token")
    if not schema_supported(batch.schema_version):
        raise HTTPException(400, f"unsupported schema_version {batch.schema_version}")
    accepted, rejected = validate_samples(batch.samples, ctx.tag_keys)
    return await asyncio.to_thread(ctx.receive, batch.batch_id, batch.source_id, accepted, to_db(datetime.now(timezone.utc)), rejected)


# --- 조회 ---------------------------------------------------------------------
@router.get("/")
async def plant_page(ctx: PlantContext = Depends(context)):
    # 디버깅 화면은 자주 바뀌므로 브라우저가 캐시하지 않게 한다
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html", headers={"Cache-Control": "no-cache"})


@router.get("/health")
async def plant_health(ctx: PlantContext = Depends(context)):
    return {"status": "ok", **ctx.health()}


@router.get("/equipment")
async def equipment(ctx: PlantContext = Depends(context)):
    """계층 순서의 설비 목록과 태그 (화면이 표를 그릴 때)."""
    return ctx.listing


@router.get("/current")
async def current_all(ctx: PlantContext = Depends(context)):
    """모든 태그의 현재값 + status. 순서는 /equipment 와 같다."""
    rows = {(r["equip_id"], r["tag_id"]): r for r in ctx.storage.query_dicts("SELECT * FROM tag_current")}
    return [ctx.with_status(rows.get((e["equip_id"], t["tag_id"])), (e["equip_id"], t["tag_id"]))
            for e in ctx.listing for t in e["tags"]]


@router.get("/equipment/{equip_id}/tags/{tag_id}/current")
async def tag_current(equip_id: str, tag_id: str, ctx: PlantContext = Depends(context)):
    key = (equip_id, tag_id)
    if key not in ctx.tag_keys:
        raise HTTPException(404, f"unknown tag {equip_id}/{tag_id}")
    return ctx.with_status(ctx.storage.current(key), key)


@router.get("/timeseries")
async def timeseries(equip_id: str, tag_id: str, from_: str = Query(alias="from"), to: str = Query(), interval: str = "1h",
                     ctx: PlantContext = Depends(context)):
    if (equip_id, tag_id) not in ctx.tag_keys:
        raise HTTPException(404, f"unknown tag {equip_id}/{tag_id}")
    if interval not in ("10m", "1h"):
        raise HTTPException(400, "interval must be 10m or 1h")
    return ctx.storage.query_dicts(
        f"SELECT equip_id, tag_id, bucket_start AS ts, avg, min, max, sd, count, status FROM ts_{interval} "
        "WHERE equip_id = ? AND tag_id = ? AND bucket_start >= ? AND bucket_start < ? ORDER BY bucket_start",
        (equip_id, tag_id, to_db(parse_iso(from_)), to_db(parse_iso(to))),
    )


@router.get("/alarms")
async def alarms(equip_id: str | None = None, from_: str | None = Query(default=None, alias="from"), to: str | None = None,
                 ctx: PlantContext = Depends(context)):
    where, params = ["1=1"], []
    if equip_id:
        where.append("equip_id = ?"); params.append(equip_id)
    if from_:
        where.append("occurred_at >= ?"); params.append(to_db(parse_iso(from_)))
    if to:
        where.append("occurred_at < ?"); params.append(to_db(parse_iso(to)))
    return ctx.storage.query_dicts(f"SELECT * FROM alarm_detail WHERE {' AND '.join(where)} ORDER BY occurred_at DESC", params)


@router.get("/maintenance")
async def maintenance(equip_id: str | None = None, ctx: PlantContext = Depends(context)):
    """정비 이력 (개발용 시나리오에서 적재). 다른 파드는 DB 를 직접 읽지 못하므로 API 로 연다."""
    where, params = ("WHERE equip_id = ?", [equip_id]) if equip_id else ("", [])
    return ctx.storage.query_dicts(f"SELECT * FROM maintenance_history {where} ORDER BY start_ts", params)


@router.get("/failures")
async def failures(equip_id: str | None = None, ctx: PlantContext = Depends(context)):
    """고장 이력 (개발용 시나리오에서 적재)."""
    where, params = ("WHERE equip_id = ?", [equip_id]) if equip_id else ("", [])
    return ctx.storage.query_dicts(f"SELECT * FROM failure_history {where} ORDER BY start_ts", params)


@router.get("/stream")
async def stream(ctx: PlantContext = Depends(context)):
    return StreamingResponse(ctx.broadcaster.subscribe(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# --- 앱 ----------------------------------------------------------------------
def create_app(plants: PlantsConfig, settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.plants = {entry.key: PlantContext.open(entry) for entry in plants.plants}
        app.state.started_at = to_db(datetime.now(timezone.utc))
        tasks: list[asyncio.Task] = []
        if settings.with_simulators:
            from collector.embedded import run_plant_simulator
            for entry in plants.plants:
                task = asyncio.create_task(run_plant_simulator(app.state.plants[entry.key], entry, settings), name=f"sim-{entry.key}")
                task.add_done_callback(_simulator_done)
                tasks.append(task)
        yield
        for task in tasks:
            task.cancel()
        for ctx in app.state.plants.values():
            ctx.close()

    app = FastAPI(title="Skadi 앞단 수집 서버", lifespan=lifespan, root_path=settings.root_path)
    app.state.plants = {}
    app.state.settings = settings
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["GET", "HEAD", "OPTIONS"],
                           allow_headers=["*"])

    @app.get("/health")
    async def root_health(request: Request):
        return {"status": "ok", "started_at": request.app.state.started_at,
                "plants": {key: ctx.health() for key, ctx in request.app.state.plants.items()}}

    @app.get("/", response_class=HTMLResponse)
    async def root_index(request: Request):
        return _plants_page(request.app.state.plants, request.scope.get("root_path", ""))

    @app.get("/{plant_key}")
    async def plant_root(plant_key: str, request: Request):
        context(plant_key, request)                 # 모르는 키면 404
        return RedirectResponse(f"{request.scope.get('root_path', '')}/{plant_key}/", status_code=307)

    app.include_router(router)
    return app


def _simulator_done(task: asyncio.Task) -> None:
    """통합 실행의 공장 태스크가 예외로 끝나면 로그를 남기고 프로세스를 끝낸다 (k8s 가 재시작, 재개 로직이 이어 채운다)."""
    if task.cancelled() or task.exception() is None:
        return
    log.error("embedded simulator %s died: %r", task.get_name(), task.exception())
    signal.raise_signal(signal.SIGTERM)


def check_plants(plants: PlantsConfig) -> None:
    """서버를 띄우기 전에 공장을 하나씩 열어 본다. 설정·DB 문제는 traceback 대신 "plant <key>: 이유" 한 줄로 알린다."""
    for entry in plants.plants:
        try:
            PlantContext.open(entry).close()
        except ConfigError as e:
            raise ConfigError(f"plant {entry.key}: {e}") from None


def create_single_app(plant_path, runtime_path, db_path, scenario_path=None, key: str = "plant",
                      settings: Settings | None = None) -> FastAPI:
    """공장 하나짜리 서버 (테스트·호환용). 경로는 /{key}/... 다."""
    entry = PlantEntry(key=key, plant=str(plant_path), runtime=str(runtime_path), db=str(db_path),
                       scenario=str(scenario_path) if scenario_path else None)
    return create_app(PlantsConfig(plants=[entry]), settings)


def _plants_page(plants: dict[str, PlantContext], root: str = "") -> str:
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
<p><a href="{root}/health">전체 상태 (/health)</a></p></body></html>"""


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
    return p.parse_args(argv)


def main(argv=None) -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv)
    plants = load_plants(args.plants)
    settings = Settings.from_env(with_simulators=args.with_simulators or None, time_mode=args.time_mode,
                                 backfill_days=args.backfill_days)
    try:
        check_plants(plants)
    except ConfigError as e:
        sys.exit(f"error: {e}")
    uvicorn.run(create_app(plants, settings), host=args.host, port=args.port, root_path=settings.root_path)


if __name__ == "__main__":
    main()
