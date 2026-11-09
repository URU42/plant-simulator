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
