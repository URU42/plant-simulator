"""예전 스키마로 만든 DB 파일에 새 수집 서버를 띄워도 깨지지 않아야 한다 (수첨탈황 구성안의 기존 data/plant.db)."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from collector.app import create_app
from common.config_models import ConfigError

# PR #1 당시 schema.sql 의 두 테이블 (equipment_tag 9컬럼, 한계값 NOT NULL)
OLD_SCHEMA = """
CREATE TABLE equipment (
    equip_id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL, grade INTEGER NOT NULL,
    unit_id TEXT NOT NULL, driven_by TEXT
);
CREATE TABLE equipment_tag (
    tag_id TEXT PRIMARY KEY, equip_id TEXT NOT NULL, signal TEXT NOT NULL, unit TEXT NOT NULL, location TEXT NOT NULL,
    warn_hi REAL NOT NULL, alarm_hi REAL NOT NULL, trip_hi REAL NOT NULL, delay_samples INTEGER NOT NULL
);
"""


def _old_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.close()
    return path


def test_hds_collector_starts_on_old_schema_db(plant_path, runtime_path, tmp_path):
    db = _old_db(tmp_path / "plant.db")
    with TestClient(create_app(plant_path, runtime_path, db)) as c:
        assert c.get("/health").json()["plant_id"] == "PL-HDS"
        tags = {t["tag_id"]: t for e in c.get("/equipment").json() for t in e["tags"]}
        assert len(tags) == 46 and tags["VI-C1101"]["decimals"] == 3
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(equipment_tag)")}
    assert {"description", "interval_sec", "decimals"} <= cols


def test_rfcc_on_old_schema_db_is_refused_with_clear_message(rfcc_plant_path, rfcc_runtime_path, tmp_path):
    db = _old_db(tmp_path / "plant.db")
    with pytest.raises(ConfigError, match="limits"):
        with TestClient(create_app(rfcc_plant_path, rfcc_runtime_path, db)):
            pass
