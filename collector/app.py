"""중앙 수집 서버 (FastAPI): /ingest, 조회 API, /stream (기획서 6.4), 디버깅 화면 / (RFCC 설계 §4.5~4.6).

실행: uvicorn collector.app:app  또는  python -m collector.app --plant config/plant_hds.yaml ...
"""
from __future__ import annotations

import argparse
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse

from collector.ingest import RawBatch, ingest, schema_supported
from collector.jobs import MAX_WINDOWS_PER_ADVANCE, JobRunner
from collector.seed_master import seed_master
from collector.storage import Storage
from collector.stream import Broadcaster
from common.config_models import PlantConfig, RuntimeConfig, load_plant, load_runtime
from common.timefmt import from_db, parse_iso, to_db

NO_DATA_SEND_INTERVALS = 3      # 전송 주기(또는 더 긴 태그 주기) 3번 동안 안 들어오면 NoData (6.2, RFCC 설계 §4.5)
STATIC_DIR = Path(__file__).with_name("static")     # 디버깅 화면


def create_app(plant_path: str | Path, runtime_path: str | Path, db_path: str | Path,
               scenario_path: str | Path | None = None) -> FastAPI:
    plant = load_plant(plant_path)
    runtime = load_runtime(runtime_path)
    equipment_listing = _equipment_listing(plant, runtime)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        storage = Storage(db_path)
        seed_master(storage, plant)
        if scenario_path:
            from collector.seed_history import seed_history   # 시나리오 → 정비·고장 이력 (개발용)
            seed_history(storage, scenario_path, plant)
        app.state.storage = storage
        app.state.tag_owner = storage.tag_owner()
        app.state.jobs = JobRunner(storage, plant, runtime)
        app.state.broadcaster = Broadcaster()
        app.state.started_at = to_db(datetime.now(timezone.utc))
        app.state.catch_up_task = None
        yield
        if app.state.catch_up_task:
            app.state.catch_up_task.cancel()
        storage.close()

    app = FastAPI(title="Skadi 앞단 수집 서버", lifespan=lifespan)
    app.state.plant = plant
    app.state.runtime = runtime

    # --- 수집 ---------------------------------------------------------------
    @app.post("/ingest")
    async def post_ingest(batch: RawBatch, request: Request):
        if not schema_supported(batch.schema_version):
            raise HTTPException(400, f"unsupported schema_version {batch.schema_version}")
        st = request.app.state
        received_at = to_db(datetime.now(timezone.utc))
        result, accepted = ingest(st.storage, batch, received_at, st.tag_owner, _oldest_allowed(st.storage.sim_now()))
        if accepted:
            if st.jobs.advance(st.storage.sim_now()) >= MAX_WINDOWS_PER_ADVANCE:
                _start_catch_up(st)
            st.broadcaster.publish("samples", [s.model_dump(mode="json") for s in accepted])
        return result

    def _start_catch_up(st) -> None:
        """밀린 구간이 상한을 넘으면 요청을 붙잡지 않고 백그라운드에서 이어서 따라잡는다."""
        if st.catch_up_task is None or st.catch_up_task.done():
            st.catch_up_task = asyncio.create_task(_catch_up(st))

    async def _catch_up(st) -> None:
        while True:
            await asyncio.sleep(0)                      # 다른 요청이 끼어들 틈을 준다
            if st.jobs.advance(st.storage.sim_now()) == 0:
                return

    # --- 조회 ---------------------------------------------------------------
    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/equipment")
    async def equipment():
        """계층 순서의 설비 목록과 태그 (화면이 표를 그릴 때)."""
        return equipment_listing

    @app.get("/current")
    async def current_all(request: Request):
        """모든 태그의 현재값 + status. 순서는 /equipment 와 같다."""
        st = request.app.state
        rows = {r["tag_id"]: r for r in st.storage.query_dicts("SELECT * FROM tag_current")}
        return [_with_status(st, rows.get(t["tag_id"]), t["tag_id"]) for e in equipment_listing for t in e["tags"]]

    @app.get("/health")
    async def health(request: Request):
        st = request.app.state
        catching_up = st.catch_up_task is not None and not st.catch_up_task.done()
        return {"status": "ok", "started_at": st.started_at, "last_received_at": st.storage.last_received_at(),
                "sim_now": st.storage.sim_now(), "catching_up": catching_up, "plant_id": plant.plant.plant_id}

    @app.get("/tags/{tag_id}/current")
    async def tag_current(tag_id: str, request: Request):
        st = request.app.state
        if tag_id not in st.tag_owner:
            raise HTTPException(404, f"unknown tag_id {tag_id}")
        row = st.storage.current(tag_id)
        return _with_status(st, row, tag_id)

    def _with_status(st, row: dict | None, tag_id: str) -> dict:
        """현재값 행에 status 를 붙인다. 행이 없거나 전송·태그 주기 중 긴 쪽의 3배를 넘게 안 들어오면 NoData."""
        if row is None:
            return {"tag_id": tag_id, "equip_id": st.tag_owner[tag_id], "ts": None, "value": None, "quality": None,
                    "received_at": None, "status": "NoData"}
        age = (from_db(st.storage.sim_now()) - from_db(row["ts"])).total_seconds()
        interval = max(runtime.send_interval_sec, plant.tag_interval_sec(tag_id, runtime.sample_interval_sec))
        return row | {"status": "NoData" if age > NO_DATA_SEND_INTERVALS * interval else row["quality"]}

    @app.get("/timeseries")
    async def timeseries(request: Request, tag_id: str, from_: str = Query(alias="from"), to: str = Query(),
                         interval: str = "1h"):
        st = request.app.state
        if tag_id not in st.tag_owner:
            raise HTTPException(404, f"unknown tag_id {tag_id}")
        if interval not in ("10m", "1h"):
            raise HTTPException(400, "interval must be 10m or 1h")
        return st.storage.query_dicts(
            f"SELECT tag_id, bucket_start AS ts, avg, min, max, sd, count, status FROM ts_{interval} "
            "WHERE tag_id = ? AND bucket_start >= ? AND bucket_start < ? ORDER BY bucket_start",
            (tag_id, to_db(parse_iso(from_)), to_db(parse_iso(to))),
        )

    @app.get("/alarms")
    async def alarms(request: Request, equip_id: str | None = None, from_: str | None = Query(default=None, alias="from"),
                     to: str | None = None):
        st = request.app.state
        where, params = ["1=1"], []
        if equip_id:
            where.append("equip_id = ?"); params.append(equip_id)
        if from_:
            where.append("occurred_at >= ?"); params.append(to_db(parse_iso(from_)))
        if to:
            where.append("occurred_at < ?"); params.append(to_db(parse_iso(to)))
        return st.storage.query_dicts(
            f"SELECT * FROM alarm_detail WHERE {' AND '.join(where)} ORDER BY occurred_at DESC", params)

    @app.get("/stream")
    async def stream(request: Request):
        return StreamingResponse(request.app.state.broadcaster.subscribe(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    def _oldest_allowed(sim_now: str | None) -> str | None:
        """시뮬레이터 현재 시각 기준 보관 기간 밖의 행은 받지 않는다. 백필을 다시 돌려도 DB가 부풀지 않는다."""
        if sim_now is None:
            return None
        return to_db(from_db(sim_now) - timedelta(days=runtime.raw_retention_days))

    return app


def _equipment_listing(plant: PlantConfig, runtime: RuntimeConfig) -> list[dict]:
    """unit_groups → units → 설비(yaml 순서) 순으로 설비와 태그를 펼친다."""
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
                              "interval_sec": plant.tag_interval_sec(t.tag_id, runtime.sample_interval_sec),
                              "decimals": t.decimals} for t in e.tags],
                })
    return out


# --- 실행 진입점 -----------------------------------------------------------
def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Skadi 앞단 수집 서버")
    p.add_argument("--plant", default=os.environ.get("PLANT_CONFIG", "config/plant_hds.yaml"))
    p.add_argument("--runtime", default=os.environ.get("RUNTIME_CONFIG", "config/runtime.yaml"))
    p.add_argument("--scenario", default=os.environ.get("SCENARIO_CONFIG"))
    p.add_argument("--db", default=os.environ.get("DB_PATH", "data/plant.db"))
    p.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    return p.parse_args(argv)


def main(argv=None) -> None:
    import uvicorn

    args = _parse_args(argv)
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    uvicorn.run(create_app(args.plant, args.runtime, args.db, args.scenario), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
