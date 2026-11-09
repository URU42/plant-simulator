"""SQLite 저장 접근부 (교체 가능).

수집 서버가 DB에 닿는 유일한 곳이다. 시계열 DB로 바꿀 때 이 모듈만 바꾼다.
시각은 common.timefmt 규칙(UTC ISO 문자열)으로 저장한다.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# 예전 스키마로 만든 DB 파일에 뒤늦게 추가된 컬럼. CREATE TABLE IF NOT EXISTS 는 기존 테이블을 바꾸지 않으므로 여기서 붙인다.
ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "equipment_tag": {"description": "TEXT", "interval_sec": "INTEGER", "decimals": "INTEGER NOT NULL DEFAULT 3"},
}


class Storage:
    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if str(db_path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.db_path = str(db_path)
        self._migrate()

    def _migrate(self) -> None:
        """예전 DB 파일에 빠진 컬럼을 끝에 덧붙인다 (순서가 schema.sql 과 다르므로 INSERT 는 컬럼 이름을 적는다)."""
        for table, columns in ADDED_COLUMNS.items():
            existing = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            for name, ddl in columns.items():
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def not_null_columns(self, table: str) -> set[str]:
        """NOT NULL 인 컬럼 이름. 예전 스키마는 한계값·등급이 NOT NULL 이었다."""
        return {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})") if r["notnull"]}

    def close(self) -> None:
        self._conn.close()

    # --- 범용 ---------------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, tuple(params))

    def executemany(self, sql: str, rows: Iterable[tuple]) -> sqlite3.Cursor:
        return self._conn.executemany(sql, rows)

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[tuple]:
        return [tuple(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def query_dicts(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def count(self, table: str) -> int:
        return self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def transaction(self) -> "_Tx":
        return _Tx(self._conn)

    # --- 수집 ---------------------------------------------------------------
    def batch_seen(self, batch_id: str) -> bool:
        return self._conn.execute("SELECT 1 FROM ingest_batch WHERE batch_id = ?", (batch_id,)).fetchone() is not None

    def tag_ids(self) -> set[str]:
        return {r[0] for r in self._conn.execute("SELECT tag_id FROM equipment_tag")}

    def tag_owner(self) -> dict[str, str]:
        """tag_id → equip_id."""
        return dict(self.query("SELECT tag_id, equip_id FROM equipment_tag"))

    def store_batch(self, batch_id: str, source_id: str, received_at: str, rows: list[tuple]) -> int:
        """rows: (tag_id, ts, equip_id, value, quality). 같은 (tag_id, ts)는 한 번만 저장하고 저장 건수를 돌려준다."""
        with self.transaction():
            before = self._conn.total_changes
            self._conn.executemany(
                "INSERT OR IGNORE INTO ts_raw (tag_id, ts, equip_id, value, quality, batch_id, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(*r, batch_id, received_at) for r in rows],
            )
            stored = self._conn.total_changes - before
            # 현재값: 측정 시각이 더 새로운 행만 반영한다 (시각 순서가 뒤바뀐 행 허용)
            self._conn.executemany(
                "INSERT INTO tag_current (tag_id, equip_id, ts, value, quality, received_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(tag_id) DO UPDATE SET equip_id = excluded.equip_id, ts = excluded.ts, value = excluded.value, "
                "quality = excluded.quality, received_at = excluded.received_at WHERE excluded.ts > tag_current.ts",
                [(r[0], r[2], r[1], r[3], r[4], received_at) for r in rows],
            )
            self._conn.execute(
                "INSERT INTO ingest_batch (batch_id, source_id, received_at, stored) VALUES (?, ?, ?, ?)",
                (batch_id, source_id, received_at, stored),
            )
        return stored

    def current(self, tag_id: str) -> dict[str, Any] | None:
        rows = self.query_dicts("SELECT * FROM tag_current WHERE tag_id = ?", (tag_id,))
        return rows[0] if rows else None

    def sim_now(self) -> str | None:
        """수신한 측정 시각의 최대값 = 수집 서버가 보는 시뮬레이터 현재 시각."""
        return self._conn.execute("SELECT MAX(ts) FROM tag_current").fetchone()[0]

    def last_received_at(self) -> str | None:
        return self._conn.execute("SELECT MAX(received_at) FROM ingest_batch").fetchone()[0]


class _Tx:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> None:
        self._conn.execute("BEGIN")

    def __exit__(self, exc_type, exc, tb) -> None:
        self._conn.execute("ROLLBACK" if exc_type else "COMMIT")
