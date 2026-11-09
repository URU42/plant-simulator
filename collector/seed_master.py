"""구성안 → 설비 마스터 적재 (기획서 7.1).

서버 시작 때 plant_hds.yaml 내용을 시설 계층·설비·유형 매핑·별칭·표준 태그·설비 태그 테이블에 넣는다.
시뮬레이터와 Skadi가 같은 ID를 보도록 매번 덮어쓴다(INSERT OR REPLACE).
"""
from __future__ import annotations

from common.config_models import Limits, PlantConfig
from collector.storage import Storage


def seed_master(storage: Storage, plant: PlantConfig) -> None:
    p = plant.plant
    with storage.transaction():
        storage.execute("INSERT OR REPLACE INTO plant VALUES (?, ?, ?)", (p.plant_id, p.name, p.config_version))
        storage.executemany("INSERT OR REPLACE INTO unit_group VALUES (?, ?, ?)",
                            [(g.id, p.plant_id, g.name) for g in p.unit_groups])
        storage.executemany("INSERT OR REPLACE INTO unit VALUES (?, ?, ?)",
                            [(u.id, g.id, u.name) for g in p.unit_groups for u in g.units])
        storage.executemany("INSERT OR REPLACE INTO equipment_type VALUES (?, ?, ?)",
                            [(t.type, t.name, t.prefix) for t in plant.equipment_types])
        storage.executemany("INSERT OR REPLACE INTO tag_std VALUES (?, ?, ?, ?)",
                            [(s.signal, s.prefix, s.unit, s.name) for s in plant.signals])
        storage.executemany("INSERT OR REPLACE INTO equipment VALUES (?, ?, ?, ?, ?, ?)",
                            [(e.equip_id, e.name, e.type, e.grade, e.unit_id, e.driven_by) for e in plant.equipment])
        storage.executemany("INSERT OR REPLACE INTO equipment_alias VALUES (?, ?)",
                            [(a, e.equip_id) for e in plant.equipment for a in e.aliases])
        storage.executemany(
            "INSERT OR REPLACE INTO equipment_tag VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(t.tag_id, e.equip_id, t.signal, t.unit, t.location, t.description,
              *(_limit_values(t.limits)), t.delay_samples, t.interval_sec, t.decimals)
             for e in plant.equipment for t in e.tags],
        )


def _limit_values(limits: Limits | None) -> tuple[float | None, float | None, float | None]:
    """한계값이 없는 태그는 NULL 세 개."""
    if limits is None:
        return None, None, None
    return limits.warn_hi, limits.alarm_hi, limits.trip_hi
