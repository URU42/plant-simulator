-- 프로젝트 DB (SQLite, WAL). 시각 컬럼은 모두 UTC ISO 문자열.
-- 스키마 v3: 태그는 (equip_id, tag_id) 복합키로 식별한다. 태그명은 같은 설비 안에서만 고유하다.
-- v3 에서 태그에 Nelson 관리 기준(control_mean·control_sigma)을 더하고 알람 지연·측정 위치 칸과 정비·고장 이력 테이블을 뺐고,
-- 한계값 알람(alarm_detail)을 Nelson 알람(nelson_alarm)으로 바꿨다.
CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL);

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
-- 한계값 여섯 칸은 자료의 참고값으로, 있는 것만 채운다. normal_ref / limit_basis 는 자료의 "정상 기준(참고)" / "한계값 근거" 원문.
-- control_mean / control_sigma 는 Nelson 관리 기준 μ·σ. interval_sec 가 NULL 이면 실행 설정의 sample_interval_sec.
CREATE TABLE IF NOT EXISTS equipment_tag (
    equip_id TEXT NOT NULL, tag_id TEXT NOT NULL, signal TEXT NOT NULL, unit TEXT NOT NULL,
    description TEXT, normal_ref TEXT, limit_basis TEXT,
    warn_lo REAL, alarm_lo REAL, trip_lo REAL, warn_hi REAL, alarm_hi REAL, trip_hi REAL,
    control_mean REAL NOT NULL, control_sigma REAL NOT NULL, interval_sec INTEGER, decimals INTEGER NOT NULL,
    PRIMARY KEY (equip_id, tag_id)
);

-- 원시 값 (7.2) — 보관 기간은 실행 설정 raw_retention_days
CREATE TABLE IF NOT EXISTS ts_raw (
    equip_id TEXT NOT NULL, tag_id TEXT NOT NULL, ts TEXT NOT NULL, value REAL, status TEXT NOT NULL,
    batch_id TEXT NOT NULL, received_at TEXT NOT NULL,
    PRIMARY KEY (equip_id, tag_id, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_ts_raw_ts ON ts_raw (ts);

-- 현재값
CREATE TABLE IF NOT EXISTS tag_current (
    equip_id TEXT NOT NULL, tag_id TEXT NOT NULL, ts TEXT NOT NULL, value REAL, status TEXT NOT NULL, received_at TEXT NOT NULL,
    PRIMARY KEY (equip_id, tag_id)
);

-- 집계 (7.3). bucket_start 는 구간 시작 시각
CREATE TABLE IF NOT EXISTS ts_10m (
    equip_id TEXT NOT NULL, tag_id TEXT NOT NULL, bucket_start TEXT NOT NULL,
    avg REAL, min REAL, max REAL, sd REAL, count INTEGER NOT NULL, status TEXT NOT NULL,
    PRIMARY KEY (equip_id, tag_id, bucket_start)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS ts_1h (
    equip_id TEXT NOT NULL, tag_id TEXT NOT NULL, bucket_start TEXT NOT NULL,
    avg REAL, min REAL, max REAL, sd REAL, count INTEGER NOT NULL, status TEXT NOT NULL,
    PRIMARY KEY (equip_id, tag_id, bucket_start)
) WITHOUT ROWID;

-- Nelson 알람 (설계 2026-11-12-nelson-anomaly §4.8). (태그, 규칙)마다 연속 위반을 한 행으로 묶는다.
-- rule 1~8. direction: hi / lo (규칙 1·2·3·5·6), 규칙 4·7·8 은 NULL. cleared_at 은 15 표본 연속 미성립의 첫 표본 시각, 열려 있으면 NULL.
-- value_at_occurrence 는 처음 성립한 표본의 값. 심각도는 두지 않는다.
CREATE TABLE IF NOT EXISTS nelson_alarm (
    alarm_id INTEGER PRIMARY KEY AUTOINCREMENT,
    equip_id TEXT NOT NULL, tag_id TEXT NOT NULL, rule INTEGER NOT NULL, direction TEXT,
    occurred_at TEXT NOT NULL, cleared_at TEXT, last_hit_at TEXT NOT NULL,
    hit_count INTEGER NOT NULL, value_at_occurrence REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_nelson_alarm_occurred ON nelson_alarm (occurred_at);
CREATE INDEX IF NOT EXISTS ix_nelson_alarm_cleared ON nelson_alarm (cleared_at);

-- 수신 묶음 (중복 확인)과 배치 진행 상태
CREATE TABLE IF NOT EXISTS ingest_batch (batch_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, received_at TEXT NOT NULL, stored INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS job_state (name TEXT PRIMARY KEY, last_bucket_end TEXT NOT NULL);
