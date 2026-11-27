"""시뮬레이션 전용 계산 서비스 (PU-M1). 수집기·SQLite·Redis·AE 없이 단독으로 뜬다.

    python -m simulator.calc_api --host 0.0.0.0 --port 8010

브라우저는 이 서비스를 직접 부르지 않는다. sk-clx Backend 만 내부 망에서 부른다. CALC_TOKEN 을 주면
X-Calc-Token 헤더가 같은 요청만 받는다(값은 Backend 와 이 서비스만 안다).
요청마다 정상 운전점에서 다시 계산하는 무상태 서비스이고, 파일·DB·스트림에 아무것도 쓰지 않는다.
"""
from __future__ import annotations

import argparse
import hmac
import logging
import os
import time

import uvicorn
from fastapi import Body, FastAPI, Header, HTTPException, Response
from fastapi.responses import JSONResponse

from simulator.dynamic_buffer import DynamicRegistry
from simulator.static_calc import PLANTS, CalcError, Registry

log = logging.getLogger("calc_api")
STATUS = {"input_error": 400, "unknown_plant": 404, "conflict": 409, "out_of_range": 422, "unsupported": 422,
          "no_solution": 422, "not_converged": 422, "engine_error": 500}


def create_app(registry: Registry | None = None, token: str | None = None, dynamic: DynamicRegistry | None = None) -> FastAPI:
    registry = registry or Registry()
    dynamic = dynamic or DynamicRegistry(registry)
    token = token if token is not None else os.environ.get("CALC_TOKEN") or None
    app = FastAPI(title="plant-simulator 정적 계산 서비스", docs_url=None, redoc_url=None, openapi_url=None)

    def check(given: str | None) -> None:
        if token is not None and not hmac.compare_digest((given or "").encode(), token.encode()):
            raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "계산 서비스 토큰이 필요합니다."})

    def plant_of(key: str):
        calc = registry.get(key)
        if calc is None:
            raise CalcError("unknown_plant", f"알 수 없는 공장: {key}", plants=list(registry.calcs))
        return calc

    @app.exception_handler(CalcError)
    async def calc_error(_, exc: CalcError):
        return JSONResponse(status_code=STATUS.get(exc.code, 500), content={"error": exc.body()})

    @app.exception_handler(HTTPException)
    async def http_error(_, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": "http_error", "message": str(exc.detail)}
        return JSONResponse(status_code=exc.status_code, content={"error": detail})

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "plants": list(registry.calcs), "synthetic": True}

    @app.get("/v1/plants")
    def plants(x_calc_token: str | None = Header(default=None)) -> dict:
        check(x_calc_token)
        return {"plants": [{"plant": k, "plant_id": c.plant.plant.plant_id, "name": c.plant.plant.name,
                            "model_version": c.model_version} for k, c in registry.calcs.items()], "synthetic": True}

    @app.get("/v1/plants/{plant}/catalog")
    def catalog(plant: str, x_calc_token: str | None = Header(default=None)) -> dict:
        check(x_calc_token)
        return plant_of(plant).catalog()

    @app.post("/v1/plants/{plant}/calculate")
    def calculate(plant: str, response: Response, body: dict = Body(...),
                  x_calc_token: str | None = Header(default=None)) -> dict:
        check(x_calc_token)
        calc = plant_of(plant)
        started = time.perf_counter()
        try:
            result = calc.calculate(body)
        except CalcError as exc:
            log.info("calculate %s -> %s (%.1f ms)", plant, exc.code, (time.perf_counter() - started) * 1000)
            raise
        # 소요 시간은 본문에 넣지 않는다(같은 입력 = 같은 본문)
        response.headers["X-Calc-Elapsed-Ms"] = f"{(time.perf_counter() - started) * 1000:.2f}"
        return result

    def dynamic_of(key: str):
        plant_of(key)
        model = dynamic.get(key.lower())
        if model is None:
            raise CalcError("unsupported", f"{key}: 시간 재생(완충 대용모델)이 없는 공장입니다. 현재 {', '.join(dynamic.models) or '없음'}만 지원합니다.")
        return model

    @app.get("/v1/plants/{plant}/dynamic")
    def dynamic_model(plant: str, x_calc_token: str | None = Header(default=None)) -> dict:
        """시간 재생 모델 설명(합성 설정값·alpha·체류 시간). 정적 카탈로그와 따로 둔다."""
        check(x_calc_token)
        model = dynamic_of(plant)
        return {"mode": "dynamic", "synthetic": True, "plant": model.calc.key, "model_version": model.version, "buffer": model.describe()}

    @app.post("/v1/plants/{plant}/dynamic")
    def dynamic_run(plant: str, response: Response, body: dict = Body(...), x_calc_token: str | None = Header(default=None)) -> dict:
        """M1 변경 목록의 타임라인을 정상 운전점부터 시간 재생한다(무상태, 결정적). 정적 계산과 결과 형식이 다르다."""
        check(x_calc_token)
        model = dynamic_of(plant)
        started = time.perf_counter()
        result = model.simulate(body)
        response.headers["X-Calc-Elapsed-Ms"] = f"{(time.perf_counter() - started) * 1000:.2f}"
        return result

    return app


def _plants(text: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in text.split(",") if p.strip())


def app_factory() -> FastAPI:
    """작업자 프로세스마다 하나씩 만든다(uvicorn --workers). 계산은 상태가 없어 프로세스끼리 나눌 것이 없다."""
    return create_app(Registry(_plants(os.environ.get("CALC_PLANTS", ",".join(PLANTS)))))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--plants", default=",".join(PLANTS))
    # 계산은 CPU 작업이라 한 프로세스 안 스레드로는 GIL 때문에 동시에 하나씩만 돈다(D12 부하: 동시 수와 무관하게 약 2.4건/초).
    # 작업자 프로세스를 늘려 처리량을 늘린다. 기본 1(기존과 같음). 프로세스당 메모리 약 60 MB.
    parser.add_argument("--workers", type=int, default=int(os.environ.get("CALC_WORKERS", "1")))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.workers > 1:
        os.environ["CALC_PLANTS"] = args.plants
        uvicorn.run("simulator.calc_api:app_factory", factory=True, workers=args.workers,
                    host=args.host, port=args.port, log_level="info")
    else:
        uvicorn.run(create_app(Registry(_plants(args.plants))), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
