"""구성안 → 설비 마스터 적재 (기획서 7.1).

서버 시작 때 구성안 내용을 시설 계층·설비·유형 매핑·별칭·표준 태그·설비 태그 테이블에 넣는다.
시뮬레이터와 Skadi가 같은 ID를 보도록 매번 덮어쓴다(INSERT OR REPLACE).
"""
from __future__ import annotations

from common.config_models import ConfigError, Limits, PlantConfig
from collector.storage import Storage


def seed_master(storage: Storage, plant: PlantConfig) -> None:
    _check_nullable(storage, plant)
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
            "INSERT OR REPLACE INTO equipment_tag (tag_id, equip_id, signal, unit, location, description, "
            "warn_hi, alarm_hi, trip_hi, delay_samples, interval_sec, decimals) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(t.tag_id, e.equip_id, t.signal, t.unit, t.location, t.description,
              *(_limit_values(t.limits)), t.delay_samples, t.interval_sec, t.decimals)
             for e in plant.equipment for t in e.tags],
        )


def _check_nullable(storage: Storage, plant: PlantConfig) -> None:
    """예전 스키마 DB 는 한계값·위치·등급이 NOT NULL 이라 그 값이 없는 구성안(RFCC)을 담을 수 없다. 새 DB 파일을 쓰라고 알린다."""
    empty: set[str] = set()
    if any(e.grade is None for e in plant.equipment):
        empty |= {"grade"} & storage.not_null_columns("equipment")
    tag_cols = storage.not_null_columns("equipment_tag")
    if any(t.limits is None for t in plant.all_tags()):
        empty |= {"warn_hi", "alarm_hi", "trip_hi"} & tag_cols
    if any(t.location is None for t in plant.all_tags()):
        empty |= {"location"} & tag_cols
    if empty:
        raise ConfigError(f"DB {storage.db_path} was created with an older schema whose columns {sorted(empty)} are NOT NULL, "
                          f"but plant {plant.plant.plant_id} leaves them empty (no limits / grade / location). Use a new DB file (--db).")


def _limit_values(limits: Limits | None) -> tuple[float | None, float | None, float | None]:
    """한계값이 없는 태그는 NULL 세 개."""
    if limits is None:
        return None, None, None
    return limits.warn_hi, limits.alarm_hi, limits.trip_hi
