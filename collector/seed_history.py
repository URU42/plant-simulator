"""시나리오 파일 → 정비 이력·고장 이력 적재 (기획서 7.1, 개발용만).

평가용 시나리오의 고장 이력은 평가 실행 때까지 적재하지 않는다(기획서 8). 수집 서버에는 개발용 파일만 준다.
"""
from __future__ import annotations

from pathlib import Path

from collector.storage import Storage
from common.config_models import PlantConfig, load_yaml
from common.scenario_models import ScenarioConfig
from common.timefmt import to_db


def seed_history(storage: Storage, scenario_path: str | Path, plant: PlantConfig | None = None) -> None:
    cfg = ScenarioConfig.model_validate(load_yaml(scenario_path))
    if plant is not None:
        cfg.validate_against(plant)
    tag_owner = dict(storage.query("SELECT tag_id, equip_id FROM equipment_tag"))
    with storage.transaction():
        storage.executemany(
            "INSERT OR REPLACE INTO maintenance_history (work_order_id, equip_id, start_ts, end_ts, description) VALUES (?, ?, ?, ?, ?)",
            [(m.work_order_id, m.equip_id, to_db(m.start), to_db(m.end), m.description) for m in cfg.maintenance])
        storage.executemany(
            "INSERT OR REPLACE INTO failure_history (failure_id, equip_id, tag_id, start_ts, end_ts, work_order_id, doc_id, description) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(e.failure_id, e.equip_id or tag_owner.get(e.tag_id), e.tag_id, to_db(e.start), to_db(e.end),
              e.work_order_id, e.doc_id, e.description) for e in cfg.events if e.failure_id])
