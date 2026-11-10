"""구성안 → 설비 마스터 적재 (기획서 7.1).

서버 시작 때 구성안 내용을 시설 계층·설비·유형 매핑·별칭·표준 태그·설비 태그 테이블에 넣는다.
시뮬레이터와 Skadi가 같은 ID를 보도록 매번 덮어쓴다(INSERT OR REPLACE). 태그는 (equip_id, tag_id) 키.
"""
from __future__ import annotations

from collector.storage import Storage
from common.config_models import Limits, PlantConfig


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
        storage.executemany(
            "INSERT OR REPLACE INTO equipment (equip_id, name, type, grade, unit_id, driven_by) VALUES (?, ?, ?, ?, ?, ?)",
            [(e.equip_id, e.name, e.type, e.grade, e.unit_id, e.driven_by) for e in plant.equipment])
        storage.executemany("INSERT OR REPLACE INTO equipment_alias VALUES (?, ?)",
                            [(a, e.equip_id) for e in plant.equipment for a in e.aliases])
        storage.executemany(
            "INSERT OR REPLACE INTO equipment_tag (equip_id, tag_id, signal, unit, location, description, normal_ref, limit_basis, "
            "warn_lo, alarm_lo, trip_lo, warn_hi, alarm_hi, trip_hi, delay_samples, interval_sec, decimals) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(e.equip_id, t.tag_id, t.signal, t.unit, t.location, t.description, t.normal_ref, t.limit_basis,
              *_limit_values(t.limits), t.delay_samples, t.interval_sec, t.decimals)
             for e in plant.equipment for t in e.tags],
        )


def _limit_values(limits: Limits | None) -> tuple:
    """(warn_lo, alarm_lo, trip_lo, warn_hi, alarm_hi, trip_hi). 한계값이 없으면 NULL 여섯 개."""
    if limits is None:
        return (None,) * 6
    return limits.warn_lo, limits.alarm_lo, limits.trip_lo, limits.warn_hi, limits.alarm_hi, limits.trip_hi
