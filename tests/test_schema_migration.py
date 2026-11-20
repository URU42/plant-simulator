"""예전 스키마(v1 태그 ID 단일 키, v2 한계값 알람용 태그 컬럼, v3 Nelson 알람·규칙 번호 사건, v4 status 칸) DB 파일은 열지 않고 명확히 알린다 (다중 공장 설계 §4.1)."""
import sqlite3

import pytest

from collector.storage import Storage
from common.config_models import ConfigError


def test_v1_db_is_refused(tmp_path):
    db = tmp_path / "plant.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE equipment_tag (tag_id TEXT PRIMARY KEY, equip_id TEXT NOT NULL)")
    conn.close()
    with pytest.raises(ConfigError, match="older schema .*Move or delete"):
        Storage(db)


def test_v2_file_is_refused(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE equipment_tag (x); CREATE TABLE schema_meta (version INTEGER); INSERT INTO schema_meta VALUES (2);")
    con.close()
    with pytest.raises(ConfigError, match="v2"):
        Storage(db)


@pytest.mark.parametrize("old", [3, 4])
def test_v3_v4_files_are_refused(tmp_path, old):
    db = tmp_path / f"v{old}.db"
    con = sqlite3.connect(db)
    con.executescript(f"CREATE TABLE equipment_tag (x); CREATE TABLE schema_meta (version INTEGER); INSERT INTO schema_meta VALUES ({old});")
    con.close()
    with pytest.raises(ConfigError, match=f"v{old}.*v5"):
        Storage(db)


def test_new_db_records_schema_version(tmp_path):
    st = Storage(tmp_path / "x.db")
    assert st.query("SELECT version FROM schema_meta") == [(5,)]
    tables = {r[0] for r in st.query("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not tables & {"maintenance_history", "failure_history", "nelson_alarm"}
    for t in ("ts_raw", "tag_current", "ts_10m", "ts_1h"):
        assert "status" not in {r[1] for r in st.query(f"PRAGMA table_info({t})")}
    st.close()
    Storage(tmp_path / "x.db").close()       # 다시 열어도 그대로


def test_check_plants_reports_v1_db_with_plant_key_in_one_line(tmp_path, plant_path, runtime_path):
    """수집 서버는 uvicorn 을 띄우기 전에 공장을 미리 열어 보고, 막히면 공장 키와 조치를 한 줄로 알린다 (리뷰 Important 3)."""
    from collector.app import check_plants
    from collector.plants import PlantEntry, PlantsConfig

    db = tmp_path / "rhds.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE equipment_tag (tag_id TEXT PRIMARY KEY, equip_id TEXT NOT NULL)")
    conn.close()
    cfg = PlantsConfig(plants=[PlantEntry(key="rhds", plant=str(plant_path), runtime=str(runtime_path), db=str(db))])
    with pytest.raises(ConfigError) as info:
        check_plants(cfg)
    msg = str(info.value)
    assert msg.startswith("plant rhds:") and "plants.yaml" in msg and "\n" not in msg
