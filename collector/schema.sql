-- 프로젝트 DB (SQLite, WAL). 시각 컬럼은 모두 UTC ISO 문자열.

-- 시설 계층 (구성안에서 적재)
CREATE TABLE IF NOT EXISTS plant (plant_id TEXT PRIMARY KEY, name TEXT NOT NULL, config_version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS unit_group (unit_group_id TEXT PRIMARY KEY, plant_id TEXT NOT NULL, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS unit (unit_id TEXT PRIMARY KEY, unit_group_id TEXT NOT NULL, name TEXT NOT NULL);

-- 설비 마스터
CREATE TABLE IF NOT EXISTS equipment_type (type TEXT PRIMARY KEY, name TEXT NOT NULL, prefix TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS equipment (
    equip_id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL, grade INTEGER,
    unit_id TEXT NOT NULL, driven_by TEXT
);
CREATE TABLE IF NOT EXISTS equipment_alias (alias TEXT PRIMARY KEY, equip_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tag_std (signal TEXT PRIMARY KEY, prefix TEXT NOT NULL, unit TEXT NOT NULL, name TEXT NOT NULL);
-- 한계값(warn/alarm/trip)은 없을 수 있다(RFCC). interval_sec 가 NULL 이면 실행 설정의 sample_interval_sec.
CREATE TABLE IF NOT EXISTS equipment_tag (
    tag_id TEXT PRIMARY KEY, equip_id TEXT NOT NULL, signal TEXT NOT NULL, unit TEXT NOT NULL,
    location TEXT, description TEXT,
    warn_hi REAL, alarm_hi REAL, trip_hi REAL, delay_samples INTEGER NOT NULL,
    interval_sec INTEGER, decimals INTEGER NOT NULL
);

-- 정비·고장 이력 (개발용 시나리오에서 적재)
CREATE TABLE IF NOT EXISTS maintenance_history (
    work_order_id TEXT PRIMARY KEY, equip_id TEXT NOT NULL, start_ts TEXT NOT NULL, end_ts TEXT NOT NULL, description TEXT
);
CREATE TABLE IF NOT EXISTS failure_history (
    failure_id TEXT PRIMARY KEY, equip_id TEXT NOT NULL, tag_id TEXT, start_ts TEXT NOT NULL, end_ts TEXT,
    work_order_id TEXT, doc_id TEXT, description TEXT
);

-- 원시 값 (7.2) — 7일 보관
CREATE TABLE IF NOT EXISTS ts_raw (
    tag_id TEXT NOT NULL, ts TEXT NOT NULL, equip_id TEXT NOT NULL, value REAL, quality TEXT NOT NULL,
    batch_id TEXT NOT NULL, received_at TEXT NOT NULL,
    PRIMARY KEY (tag_id, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_ts_raw_ts ON ts_raw (ts);

-- 현재값
CREATE TABLE IF NOT EXISTS tag_current (
    tag_id TEXT PRIMARY KEY, equip_id TEXT NOT NULL, ts TEXT NOT NULL, value REAL, quality TEXT NOT NULL, received_at TEXT NOT NULL
);

-- 집계 (7.3). bucket_start 는 구간 시작 시각
CREATE TABLE IF NOT EXISTS ts_10m (
    tag_id TEXT NOT NULL, bucket_start TEXT NOT NULL, equip_id TEXT NOT NULL,
    avg REAL, min REAL, max REAL, sd REAL, count INTEGER NOT NULL, status TEXT NOT NULL,
    PRIMARY KEY (tag_id, bucket_start)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS ts_1h (
    tag_id TEXT NOT NULL, bucket_start TEXT NOT NULL, equip_id TEXT NOT NULL,
    avg REAL, min REAL, max REAL, sd REAL, count INTEGER NOT NULL, status TEXT NOT NULL,
    PRIMARY KEY (tag_id, bucket_start)
) WITHOUT ROWID;

-- 알람 (7.3)
CREATE TABLE IF NOT EXISTS alarm_detail (
    alarm_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL, cleared_at TEXT,
    equip_id TEXT NOT NULL, tag_id TEXT NOT NULL,
    limit_type TEXT NOT NULL,          -- Soft / Alarm / Trip
    limit_value REAL NOT NULL, actual_value REAL NOT NULL, exceed_pct REAL NOT NULL,
    severity TEXT NOT NULL             -- Low / Medium / High
);
CREATE INDEX IF NOT EXISTS ix_alarm_equip ON alarm_detail (equip_id, occurred_at);

-- 수신 묶음 (중복 확인)과 배치 진행 상태
CREATE TABLE IF NOT EXISTS ingest_batch (batch_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, received_at TEXT NOT NULL, stored INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS job_state (name TEXT PRIMARY KEY, last_bucket_end TEXT NOT NULL);
