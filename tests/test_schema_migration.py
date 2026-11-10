"""예전 스키마(v1, 태그 ID 단일 키) DB 파일은 열지 않고 명확히 알린다 (다중 공장 설계 §4.1)."""
import sqlite3

import pytest

from collector.storage import Storage
from common.config_models import ConfigError


def test_v1_db_is_refused(tmp_path):
    db = tmp_path / "plant.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE equipment_tag (tag_id TEXT PRIMARY KEY, equip_id TEXT NOT NULL)")
    conn.close()
    with pytest.raises(ConfigError, match="older schema .*composite tag key.*Move or delete"):
        Storage(db)


def test_new_db_records_schema_version(tmp_path):
    st = Storage(tmp_path / "x.db")
    assert st.query("SELECT version FROM schema_meta") == [(2,)]
    st.close()
    Storage(tmp_path / "x.db").close()       # 다시 열어도 그대로


def test_check_plants_reports_v1_db_with_plant_key_in_one_line(tmp_path, plant_path, runtime_path):
    """수집 서버는 uvicorn 을 띄우기 전에 공장을 미리 열어 보고, 막히면 공장 키와 조치를 한 줄로 알린다 (리뷰 Important 3)."""
    from collector.app import check_plants
    from collector.plants import PlantEntry, PlantsConfig

    db = tmp_path / "hds.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE equipment_tag (tag_id TEXT PRIMARY KEY, equip_id TEXT NOT NULL)")
    conn.close()
    cfg = PlantsConfig(plants=[PlantEntry(key="hds", plant=str(plant_path), runtime=str(runtime_path), db=str(db))])
    with pytest.raises(ConfigError) as info:
        check_plants(cfg)
    msg = str(info.value)
    assert msg.startswith("plant hds:") and "plants.yaml" in msg and "\n" not in msg
