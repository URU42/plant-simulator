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


def _old_db(path, version):
    con = sqlite3.connect(path)
    con.executescript(f"CREATE TABLE equipment_tag (x); CREATE TABLE schema_meta (version INTEGER); INSERT INTO schema_meta VALUES ({version});"
                      "CREATE TABLE ts_raw (old_row TEXT); INSERT INTO ts_raw VALUES ('v-old');")
    con.close()


def test_newer_schema_is_refused(tmp_path):
    """예전 이미지로 되돌렸을 때(DB 가 더 새 버전) 실행 중 오류 대신 시작에서 멈춘다."""
    _old_db(tmp_path / "new.db", 6)
    with pytest.raises(ConfigError, match="newer schema \\(v6\\).*v5"):
        Storage(tmp_path / "new.db")


@pytest.mark.parametrize("found", [3, 6])
def test_reset_on_schema_change_recreates_file(tmp_path, found):
    """reset_on_schema_change: 버전이 다른 파일(과 WAL·SHM)을 지우고 새 스키마로 만든다. 합성 데이터라 백필로 다시 채운다."""
    db = tmp_path / "plant.db"
    _old_db(db, found)
    (tmp_path / "plant.db-wal").write_bytes(b"stale")
    (tmp_path / "plant.db-shm").write_bytes(b"stale")
    st = Storage(db, reset_on_schema_change=True)
    assert st.query("SELECT version FROM schema_meta") == [(5,)]
    assert "old_row" not in {r[1] for r in st.query("PRAGMA table_info(ts_raw)")}
    st.close()


def test_reset_keeps_current_schema_file(tmp_path):
    st = Storage(tmp_path / "x.db")
    st.execute("INSERT INTO ingest_batch VALUES ('b1', 's', '2026-11-20T00:00:00Z', 1)")
    st.close()
    st = Storage(tmp_path / "x.db", reset_on_schema_change=True)
    assert st.query("SELECT batch_id FROM ingest_batch") == [("b1",)]
    st.close()


def test_check_plants_resets_old_db_when_enabled(tmp_path, plant_path, runtime_path):
    from collector.app import check_plants
    from collector.plants import PlantEntry, PlantsConfig

    db = tmp_path / "rhds.db"
    _old_db(db, 3)
    check_plants(PlantsConfig(plants=[PlantEntry(key="rhds", plant=str(plant_path), runtime=str(runtime_path), db=str(db))]),
                 reset_db=True)
    assert Storage(db).query("SELECT version FROM schema_meta") == [(5,)]


def test_reset_setting_from_env(monkeypatch):
    from collector.settings import Settings
    assert Settings.from_env().reset_db_on_schema_change is False
    monkeypatch.setenv("RESET_DB_ON_SCHEMA_CHANGE", "1")
    assert Settings.from_env().reset_db_on_schema_change is True
