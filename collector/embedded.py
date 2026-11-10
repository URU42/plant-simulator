"""통합 실행 (k8s 설계 §4.1): 수집 서버 프로세스 안에서 공장별 생성기를 돌린다.

HTTP 대신 LocalSender 가 그 공장의 PlantContext.receive 를 바로 부른다. 백필 창은 10분으로 짧게 두고
창마다 이벤트 루프에 양보하므로 첫 백필 중에도 조회 API 와 프로브가 응답한다.
평가용(비공개) 시나리오는 SIM_SCENARIO_DIR 의 scenario_<key>.yaml 로 시뮬레이터에만 준다 (기획서 8장).
"""
from __future__ import annotations

import logging
from pathlib import Path

from collector.plant_context import PlantContext
from collector.plants import PlantEntry
from collector.settings import Settings
from simulator.main import run_mode
from simulator.scenario import load_scenarios
from simulator.sender import LocalSender

log = logging.getLogger("collector.embedded")

EMBEDDED_BACKFILL_WINDOW_SEC = 600     # 1초 공장 약 4만 샘플 ≈ 창당 0.5초 이하


def sim_scenario_paths(settings: Settings, key: str) -> list[str]:
    """시뮬레이터에만 주는 시나리오 파일. 없으면 빈 목록."""
    if not settings.sim_scenario_dir:
        return []
    path = Path(settings.sim_scenario_dir) / f"scenario_{key}.yaml"
    return [str(path)] if path.is_file() else []


async def run_plant_simulator(ctx: PlantContext, entry: PlantEntry, settings: Settings) -> None:
    """공장 하나의 생성기를 time_mode 대로 끝없이 돌린다."""
    runtime = ctx.runtime
    overrides = {k: v for k, v in {"time_mode": settings.time_mode, "backfill_days": settings.backfill_days}.items() if v is not None}
    if overrides:
        runtime = runtime.model_validate(runtime.model_dump() | overrides)
    private = sim_scenario_paths(settings, entry.key)
    ctx.simulator_scenarios = private
    paths = ([entry.scenario] if entry.scenario else []) + private
    scenario = load_scenarios(paths, ctx.plant, runtime) if paths else None
    log.info("plant %s: embedded simulator %s (scenarios: %s)", entry.key, runtime.time_mode, paths or "none")
    await run_mode(ctx.plant, runtime, scenario, LocalSender(ctx.receive_local, ctx.storage.sim_now),
                   window_sec=EMBEDDED_BACKFILL_WINDOW_SEC)
