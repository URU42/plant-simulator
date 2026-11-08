"""중앙 수집 서버 (FastAPI): /ingest, 조회 API, /stream (기획서 6.4).

실행: uvicorn collector.app:app  또는  python -m collector.app --plant config/plant_hds.yaml ...
"""
from __future__ import annotations

import argparse
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from collector.ingest import RawBatch, ingest, schema_supported
from collector.jobs import JobRunner
from collector.seed_master import seed_master
from collector.storage import Storage
from collector.stream import Broadcaster
from common.config_models import load_plant, load_runtime
from common.timefmt import from_db, parse_iso, to_db

NO_DATA_SEND_INTERVALS = 3      # 전송 주기 3번 동안 안 들어오면 NoData (6.2)


def create_app(plant_path: str | Path, runtime_path: str | Path, db_path: str | Path,
               scenario_path: str | Path | None = None) -> FastAPI:
    plant = load_plant(plant_path)
    runtime = load_runtime(runtime_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        storage = Storage(db_path)
        seed_master(storage, plant)
        if scenario_path:
            from collector.seed_history import seed_history   # 시나리오 → 정비·고장 이력 (개발용)
            seed_history(storage, scenario_path)
        app.state.storage = storage
        app.state.tag_owner = storage.tag_owner()
        app.state.jobs = JobRunner(storage, plant, runtime)
        app.state.broadcaster = Broadcaster()
        app.state.started_at = to_db(datetime.now(timezone.utc))
        yield
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
        result, accepted = ingest(st.storage, batch, received_at, st.tag_owner)
        if accepted:
            st.jobs.advance(st.storage.sim_now())
            st.broadcaster.publish("samples", [s.model_dump(mode="json") for s in accepted])
        return result

    # --- 조회 ---------------------------------------------------------------
    @app.get("/health")
    async def health(request: Request):
        st = request.app.state
        return {"status": "ok", "started_at": st.started_at, "last_received_at": st.storage.last_received_at(),
                "sim_now": st.storage.sim_now(), "plant_id": plant.plant.plant_id}

    @app.get("/tags/{tag_id}/current")
    async def tag_current(tag_id: str, request: Request):
        st = request.app.state
        if tag_id not in st.tag_owner:
            raise HTTPException(404, f"unknown tag_id {tag_id}")
        row = st.storage.current(tag_id)
        if row is None:
            return {"tag_id": tag_id, "equip_id": st.tag_owner[tag_id], "ts": None, "value": None,
                    "quality": None, "status": "NoData"}
        sim_now = from_db(st.storage.sim_now())
        age = (sim_now - from_db(row["ts"])).total_seconds()
        stale = age > NO_DATA_SEND_INTERVALS * runtime.send_interval_sec
        return row | {"status": "NoData" if stale else row["quality"]}

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

    return app


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
