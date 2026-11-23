-- 프로젝트 DB (SQLite, WAL). 시각 컬럼은 모두 UTC ISO 문자열.
-- 스키마 v3: 태그는 (equip_id, tag_id) 복합키로 식별한다. 태그명은 같은 설비 안에서만 고유하다.
-- v3 에서 태그에 Nelson 관리 기준(control_mean·control_sigma)을 더하고 알람 지연·측정 위치 칸과 정비·고장 이력 테이블을 뺐다.
-- v4 에서 수집 서버의 Nelson 판정(nelson_alarm)을 빼고 사건 장부를 고장 모드 주입용으로 바꿨다. control_mean 은 운전점을 반영한 값.
-- v5 에서 status(Good/Bad/NoData) 칸을 뺐다. 설비 상태는 탐지·ML 이 판단한다. 집계 구간에 표본이 없으면 count 0.
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
    equip_id TEXT NOT NULL, tag_id TEXT NOT NULL, ts TEXT NOT NULL, value REAL,
    batch_id TEXT NOT NULL, received_at TEXT NOT NULL,
    PRIMARY KEY (equip_id, tag_id, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_ts_raw_ts ON ts_raw (ts);

-- 현재값
CREATE TABLE IF NOT EXISTS tag_current (
    equip_id TEXT NOT NULL, tag_id TEXT NOT NULL, ts TEXT NOT NULL, value REAL, received_at TEXT NOT NULL,
    PRIMARY KEY (equip_id, tag_id)
);

-- 집계 (7.3). bucket_start 는 구간 시작 시각
CREATE TABLE IF NOT EXISTS ts_10m (
    equip_id TEXT NOT NULL, tag_id TEXT NOT NULL, bucket_start TEXT NOT NULL,
    avg REAL, min REAL, max REAL, sd REAL, count INTEGER NOT NULL,
    PRIMARY KEY (equip_id, tag_id, bucket_start)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS ts_1h (
    equip_id TEXT NOT NULL, tag_id TEXT NOT NULL, bucket_start TEXT NOT NULL,
    avg REAL, min REAL, max REAL, sd REAL, count INTEGER NOT NULL,
    PRIMARY KEY (equip_id, tag_id, bucket_start)
) WITHOUT ROWID;

-- 수신 묶음 (중복 확인)과 배치 진행 상태
CREATE TABLE IF NOT EXISTS ingest_batch (batch_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, received_at TEXT NOT NULL, stored INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS job_state (name TEXT PRIMARY KEY, last_bucket_end TEXT NOT NULL);

-- 이상 사건 장부 = 정답지. 주입한 고장 모드(config/fault_modes.yaml)만 적는다 (정상 운전에는 사건이 없다).
-- kind·cause·action 은 고장 모드의 정답 분류, expect 는 평가용 기대값 JSON(없으면 NULL),
-- plan 은 주입 당시의 단계({id,name,at_sec,nelson_quiet})·태그별 효과 시작({equip_id,tag_id,stage,start_sec})·unobserved(태그가 없어 못 만든 신호) JSON.
-- stop_requested_at 은 멈춤 시각(멈추지 않았으면 NULL), repair_requested_at 은 손상 수리 시각(안 고쳤으면 NULL).
-- repair_requested_at 은 v5 에 나중에 더한 열이라 기존 파일에는 Storage 가 ALTER TABLE 로 붙인다 (데이터 보존).
CREATE TABLE IF NOT EXISTS anomaly_event (
    event_id TEXT PRIMARY KEY, equip_id TEXT NOT NULL, mode TEXT NOT NULL,
    kind TEXT NOT NULL, cause TEXT NOT NULL, action TEXT NOT NULL, expect TEXT, plan TEXT NOT NULL,
    start_at TEXT NOT NULL, stop_requested_at TEXT, repair_requested_at TEXT
);
