"""SQLite 저장 접근부 (교체 가능).

수집 서버가 DB에 닿는 유일한 곳이다. 시계열 DB로 바꿀 때 이 모듈만 바꾼다.
시각은 common.timefmt 규칙(UTC ISO 문자열)으로 저장한다. 태그는 (equip_id, tag_id) 복합키.
스키마 버전은 schema_meta 에 적는다. 예전 버전(v1, 태그 ID 단일 키) 파일은 바꿀 수 없으므로 열지 않고 알린다.
연결 하나를 이벤트 루프 스레드(조회)와 워커 스레드(통합 실행의 저장·배치)가 함께 쓰므로 모든 접근을 재진입 락으로 직렬화한다.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from common.config_models import ConfigError, TagKey

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 2


class Storage:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if self.db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._check_version()
        self._conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        if self._conn.execute("SELECT COUNT(*) FROM schema_meta").fetchone()[0] == 0:
            self._conn.execute("INSERT INTO schema_meta (version) VALUES (?)", (SCHEMA_VERSION,))

    def _check_version(self) -> None:
        """테이블이 이미 있는 파일은 버전이 맞아야 한다. 새 파일은 그냥 지나간다."""
        tables = {r[0] for r in self._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "equipment_tag" not in tables:
            return
        found = self._conn.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0] if "schema_meta" in tables else 1
        if found is None or found < SCHEMA_VERSION:
            self._conn.close()
            raise ConfigError(f"DB {self.db_path} was created by an older schema (v{found or 1}); this version needs "
                              f"v{SCHEMA_VERSION} (composite tag key). Move or delete the file, or point this plant's "
                              f"db in plants.yaml to a new file")

    def close(self) -> None:
        self._conn.close()

    # --- 범용 ---------------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def executemany(self, sql: str, rows: Iterable[tuple]) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.executemany(sql, rows)

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[tuple]:
        with self._lock:
            return [tuple(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def query_dicts(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def count(self, table: str) -> int:
        with self._lock:
            return self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def transaction(self) -> "_Tx":
        """BEGIN~COMMIT 동안 락을 쥔다. 다른 스레드의 읽기는 트랜잭션이 끝날 때까지 기다린다."""
        return _Tx(self._conn, self._lock)

    # --- 수집 ---------------------------------------------------------------
    def batch_seen(self, batch_id: str) -> bool:
        return self.query("SELECT 1 FROM ingest_batch WHERE batch_id = ?", (batch_id,)) != []

    def tag_keys(self) -> set[TagKey]:
        """마스터에 있는 (equip_id, tag_id)."""
        return {(r[0], r[1]) for r in self.query("SELECT equip_id, tag_id FROM equipment_tag")}

    def store_batch(self, batch_id: str, source_id: str, received_at: str, rows: list[tuple]) -> int:
        """rows: (equip_id, tag_id, ts, value, quality). 같은 키·시각은 한 번만 저장하고 저장 건수를 돌려준다."""
        with self.transaction():
            before = self._conn.total_changes
            self._conn.executemany(
                "INSERT OR IGNORE INTO ts_raw (equip_id, tag_id, ts, value, quality, batch_id, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(*r, batch_id, received_at) for r in rows],
            )
            stored = self._conn.total_changes - before
            # 현재값: 측정 시각이 더 새로운 행만 반영한다 (시각 순서가 뒤바뀐 행 허용)
            self._conn.executemany(
                "INSERT INTO tag_current (equip_id, tag_id, ts, value, quality, received_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(equip_id, tag_id) DO UPDATE SET ts = excluded.ts, value = excluded.value, "
                "quality = excluded.quality, received_at = excluded.received_at WHERE excluded.ts > tag_current.ts",
                [(*r, received_at) for r in rows],
            )
            self._conn.execute(
                "INSERT INTO ingest_batch (batch_id, source_id, received_at, stored) VALUES (?, ?, ?, ?)",
                (batch_id, source_id, received_at, stored),
            )
        return stored

    def current(self, key: TagKey) -> dict[str, Any] | None:
        rows = self.query_dicts("SELECT * FROM tag_current WHERE equip_id = ? AND tag_id = ?", key)
        return rows[0] if rows else None

    def sim_now(self) -> str | None:
        """수신한 측정 시각의 최대값 = 수집 서버가 보는 시뮬레이터 현재 시각."""
        return self.query("SELECT MAX(ts) FROM tag_current")[0][0]

    def last_received_at(self) -> str | None:
        return self.query("SELECT MAX(received_at) FROM ingest_batch")[0][0]


class _Tx:
    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn, self._lock = conn, lock

    def __enter__(self) -> None:
        self._lock.acquire()
        self._conn.execute("BEGIN")

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self._conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self._lock.release()
