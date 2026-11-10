# 다중 공장 시뮬레이터 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CDU·HOU·RHDS·VRDS 구성안을 추가하고, 수집 서버 하나가 여섯 공장(hds, rfcc, cdu, hou, rhds, vrds)을 `/{공장}/…` 아래에 같은 모양으로 서빙하며, 데이터 키를 (설비 ID, 태그명) 복합키로 바꾼다.

**Architecture:** 구성안·메시지·DB·API 전반에서 태그를 `TagKey = (equip_id, tag_id)` 로 식별한다. 수집 서버는 `config/plants.yaml` 의 공장 목록으로 공장마다 `PlantContext`(설정·DB·배치·브로드캐스터)를 만들고, 라우트는 `/{plant_key}` 접두어 하나로 정의해 의존성이 컨텍스트를 고른다. 새 공장 구성안은 각 Excel 마스터 표를 일회용 스크립트로 변환하고, 신호 모델은 RFCC 모델에 두 종류(차압·공정 온도)와 fan 만 더한다.

**Tech Stack:** Python 3.11, NumPy, Pydantic v2, FastAPI, SQLite. 구성안 생성 스크립트(스크래치)만 openpyxl.

**Spec:** `docs/superpowers/specs/2026-11-09-multi-plant-design.md` (이전: `2026-11-09-rfcc-simulator-design.md`, 기획서 `plant-simulator-plan.md`)

## Global Constraints

- 기존 의존성만 쓴다. openpyxl 은 커밋하지 않는 일회용 스크립트에서만.
- 코드는 설비·태그 ID 형식을 해석하지 않는다. 공장 접두어는 `plants.yaml` 의 `key`.
- 값은 `(seed, seed_key, 측정 시각)` 의 순수 함수. 기존 HDS·RFCC 의 생성값은 바뀌지 않는다(시드 키 그대로).
- 마스터 값(계층·설비·태그명·설명·단위·주기·정상 기준·한계값·근거)은 Excel `설비 설명`·`설비 태그` 시트 그대로. 빈 칸은 쓰지 않는다.
- 품질 표기: `Good` / `Uncertain` / `Bad` / `ScanOff`.
- 스키마 v2. 예전 DB 파일은 명확한 `ConfigError` 로 멈춘다.
- 샘플 폴더 `cdu-sample/`, `hou-plant-sample/`, `RHDS_VRDS_v3/` 는 `.gitignore`.
- 커밋 메시지는 한국어, 끝에 `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- 모듈 머리 한 문단 한국어 docstring, 함수·데이터에 짧은 주석, 책임별 파일 분리.

## Review Focus

1. 평범한 태그명 참조(`tag_id: TI-B-3101`)가 RHDS 에서 세 설비에 걸릴 때 조용히 하나를 고르면 안 된다 → Task 1 `test_ambiguous_plain_tag_ref_names_candidates`.
2. 한 공장에 보낸 묶음이 다른 공장의 DB·현재값·SSE 에 섞이면 안 된다 → Task 4 `test_two_plants_are_isolated`.
3. 하한 알람이 해제될 때 상한 알람 키와 섞여 엉뚱한 알람이 닫히면 안 된다 → Task 3 `test_lo_and_hi_alarms_are_independent`.
4. 예전 v1 DB 파일(복합키 아님)을 열면 한 줄로 이유와 조치를 알려 주고 멈춰야 한다 → Task 3 `test_v1_db_is_refused`.
5. `Uncertain` 샘플이 NoData/Bad 로 보이거나 집계에서 빠지면 안 된다 → Task 3 `test_uncertain_counts_as_value`.

---

### Task 1: 구성안 모델 — 복합키, TagRef, 상·하한 한계값, 품질, 마스터 참고 필드

**Files:**
- Modify: `common/config_models.py`, `common/message.py`, `common/scenario_models.py`
- Test: `tests/test_config.py`, `tests/test_message.py`, `tests/test_scenario.py`

**Interfaces:**
- Produces (`common/config_models.py`):
  - `TagKey = tuple[str, str]` (equip_id, tag_id)
  - `Limits`: `warn_hi, alarm_hi, trip_hi, warn_lo, alarm_lo, trip_lo: float | None = None`. 검증: 최소 하나, `warn_hi < alarm_hi < trip_hi`(있는 것끼리), `trip_lo < alarm_lo < warn_lo`, 모든 lo < 모든 hi. 도우미 `Limits.hi_levels() -> list[tuple[str, float]]`, `lo_levels()` — `[("Soft", v), ("Alarm", v), ("Trip", v)]` 중 있는 것.
  - `TagConfig.normal_ref: str | None = None`, `TagConfig.limit_basis: str | None = None`
  - `PlantConfig`: `tag_keys() -> list[TagKey]`, `tag_by_key() -> dict[TagKey, TagConfig]`, `tag_unit(key: TagKey) -> str`, `tag_interval_sec(key: TagKey, default: int) -> int`, `resolve(ref: str, equip_hint: str | None = None) -> TagKey`. `tag_owner()`/`tag_by_id()` 삭제. 고유성: `(equip_id, tag_id)`; 태그 `seed_key` 는 공장 안에서 고유(그대로).
  - `resolve`: `"EQUIP/TAG"` → 그 키(없으면 `ConfigError`). 평범한 `"TAG"` → `equip_hint` 설비에 있으면 그것, 아니면 공장 전체에서 유일할 때 그것, 둘 이상이면 `ConfigError(f"tag {ref} is ambiguous: {E1}/{ref}, {E2}/{ref}, …")`.
  - `Correlation.source/target` 은 TagRef 문자열. `_cross_check` 가 `resolve` 로 확인하고 같은 Unit 검사.
- Produces (`common/message.py`): `Quality = Literal["Good", "Uncertain", "Bad", "ScanOff"]`; `Good`·`Uncertain` 은 값 필수.
- Produces (`common/scenario_models.py`):
  - `ScenarioEvent.equip_id: str | None`, `tag_id: str | None` — 최소 하나. `validate_against(plant)` 가 `equip_id` 가 비면 `plant.resolve(tag_id)` 로 채운다(겹치면 `ConfigError`).
  - `RelatedTag.equip_id: str | None = None`; `validate_against` 가 `plant.resolve(r.tag_id, equip_hint=event.equip_id)` 로 채운다.
  - `ScenarioEvent.target_keys(plant) -> list[TagKey]`: `tag_id` 있으면 한 개, 없으면 설비의 모든 태그.
  - `RandomQuality(uncertain_rate: float = 0, bad_segments_per_tag_week: float = 0, bad_duration: tuple[timedelta, timedelta] = (30s, 400s))` + `ScenarioConfig.random_quality: RandomQuality = RandomQuality()`. 기간 문자열 파싱 재사용.

- [ ] **Step 1: 실패하는 테스트**

`tests/test_config.py` 에 추가:

```python
def _two_equipment_same_tag():
    raw = _rfcc_like()
    raw["equipment"][0]["tags"][0]["limits"] = {"warn_lo": 3.5}
    raw["equipment"].append({"equip_id": "F-1", "name": "fan", "type": "PM", "unit_id": "U-1", "seed_key": "e2",
                             "tags": [{"tag_id": "TI-P-1", "signal": "bearing_temp", "unit": "degC", "seed_key": "r2",
                                       "normal": {"base": 50, "noise_sd": 0.03}, "normal_ref": "44~60", "limit_basis": "가정값"}]})
    return raw


def test_tag_ids_only_need_to_be_unique_per_equipment():
    plant = PlantConfig.model_validate(_two_equipment_same_tag())
    assert plant.tag_keys() == [("P-1", "TI-P-1"), ("F-1", "TI-P-1")]
    assert plant.tag_by_key()[("F-1", "TI-P-1")].normal_ref == "44~60"
    assert plant.tag_interval_sec(("F-1", "TI-P-1"), 1) == 1 and plant.tag_interval_sec(("P-1", "TI-P-1"), 1) == 10


def test_resolve_tag_refs():
    plant = PlantConfig.model_validate(_two_equipment_same_tag())
    assert plant.resolve("F-1/TI-P-1") == ("F-1", "TI-P-1")
    assert plant.resolve("TI-P-1", equip_hint="F-1") == ("F-1", "TI-P-1")
    with pytest.raises(ConfigError, match="ambiguous: F-1/TI-P-1, P-1/TI-P-1|ambiguous: P-1/TI-P-1, F-1/TI-P-1"):
        plant.resolve("TI-P-1")
    with pytest.raises(ConfigError, match="X-9/TI-P-1"):
        plant.resolve("X-9/TI-P-1")


def test_limits_allow_lower_bounds_and_check_order():
    Limits(warn_lo=2.02, alarm_lo=1.62)
    Limits(alarm_lo=115, alarm_hi=140)
    assert Limits(warn_lo=2.0, alarm_lo=1.5, warn_hi=5.0).lo_levels() == [("Soft", 2.0), ("Alarm", 1.5)]
    with pytest.raises(ValueError):
        Limits(warn_lo=1.5, alarm_lo=2.0)        # 하한은 trip < alarm < warn
    with pytest.raises(ValueError):
        Limits(warn_lo=5.0, warn_hi=4.0)         # lo 는 hi 보다 작다
    with pytest.raises(ValueError):
        Limits()
```

`tests/test_message.py` 에 추가:

```python
def test_uncertain_quality_requires_value():
    Sample(equip_id="E", tag_id="T", ts=datetime(2026, 1, 1, tzinfo=timezone.utc), value=1.0, quality="Uncertain")
    with pytest.raises(ValidationError):
        Sample(equip_id="E", tag_id="T", ts=datetime(2026, 1, 1, tzinfo=timezone.utc), value=None, quality="Uncertain")
```

`tests/test_scenario.py` 에 추가 (기존 `_ramp_event` 는 `tag_id` 만 있음 → 그대로 통과해야 한다):

```python
def test_event_with_only_tag_id_gets_equipment_filled(plant_path):
    plant = load_plant(plant_path)
    cfg = ScenarioConfig.model_validate({"events": [_ramp_event(related=[{"tag_id": "TI-C1101", "ratio": 0.5}])]})
    cfg.validate_against(plant)
    assert cfg.events[0].equip_id == "C-1101" and cfg.events[0].related[0].equip_id == "C-1101"
    assert cfg.events[0].target_keys(plant) == [("C-1101", "VI-C1101")]


def test_event_needs_equip_or_tag():
    with pytest.raises(ValueError, match="equip_id or tag_id"):
        ScenarioEvent(event_id="E", pattern="ramp", start=datetime(2026, 1, 1, tzinfo=KST), duration="1h")


def test_random_quality_config_parses_durations():
    cfg = ScenarioConfig.model_validate({"random_quality": {"uncertain_rate": 0.00003, "bad_segments_per_tag_week": 1.0,
                                                            "bad_duration": ["30s", "400s"]}})
    assert cfg.random_quality.bad_duration == (timedelta(seconds=30), timedelta(seconds=400))
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_config.py tests/test_message.py tests/test_scenario.py -q`
Expected: 새 테스트 FAIL (`tag_keys` 없음, `Uncertain` 허용 안 됨, `equip_id or tag_id` 메시지 없음, `random_quality` extra forbidden). 기존 테스트 중 `test_tag_ids_and_seed_keys_are_unique` 는 `tag_by_id` 를 쓰지 않으므로 그대로.

- [ ] **Step 3: 구현**

Interfaces 대로. `_cross_check` 의 중복 검사는 `(equip_id, tag_id)` 로. `tag_owner()` 를 쓰던 곳은 Task 2·3 에서 고친다(이 단계에서는 import 오류가 날 수 있으나 Task 1 의 세 테스트 파일만 돌린다).

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_config.py tests/test_message.py tests/test_scenario.py -q`
Expected: 세 파일 통과(시나리오 적용 테스트는 Task 2 전까지 `tag_owner` 오류로 실패할 수 있다. 그렇다면 `tests/test_scenario.py -k "parse or filled or needs or random_quality_config"` 만 확인하고 넘어간다).

- [ ] **Step 5: Commit**

```bash
git add common/config_models.py common/message.py common/scenario_models.py tests/test_config.py tests/test_message.py tests/test_scenario.py
git commit -m "feat: 구성안 복합키 (설비 ID, 태그명), TagRef, 상·하한 한계값, Uncertain 품질, 마스터 참고 필드"
```

---

### Task 2: 시뮬레이터 — TagKey 전환, 신호 종류 추가, 우연 품질 저하

**Files:**
- Modify: `simulator/signal_model.py`, `simulator/rfcc_model.py`, `simulator/generator.py`, `simulator/scenario.py`, `simulator/random_anomalies.py`
- Create: `simulator/random_quality.py`
- Test: `tests/test_signal_model.py`, `tests/test_rfcc_model.py`, `tests/test_rfcc_generator.py`, `tests/test_scenario.py`, `tests/test_simulator_run.py`, `tests/test_backfill.py`

**Interfaces:**
- Consumes: Task 1 의 `TagKey`, `tag_by_key`, `resolve`, `target_keys`, `RandomQuality`.
- Produces:
  - `SignalModel.normal_values(key: TagKey, epochs)`, `stopped_values(key, epochs)`, `values(key, epochs, deviation=None)`; `DeviationFn = Callable[[TagKey, np.ndarray], np.ndarray]`. 연관 규칙은 `plant.resolve` 로 키를 만든다.
  - `RfccSignalModel`: 같은 시그니처. `SIGNAL_KINDS` 에 `differential_pressure`, `process_temp` 추가. `process_temp` 는 `TEMPERATURES` 에 포함(식·정지값 25 °C 같음). `differential_pressure` 식 `b·(1 + 0.2·local) + z`, 정지 0. `AIR_SHAFT_KINDS = {"blower", "expander", "fan"}`.
  - `Scenario.deviation(key, epochs)`, `Scenario.apply(model, equip, key, epochs)`. 내부 사전은 키 기준. `RandomAnomalyMaker.offset(key, epochs)` — `warn_hi` 가 없으면 0.
  - `simulator/random_quality.py`: `RandomQualityMaker(plant, seed, cfg: RandomQuality)`, `qualities(key: TagKey, epochs: np.ndarray, interval_sec: int) -> np.ndarray | None` (dtype object 의 품질 배열, 아무 것도 없으면 None). (태그, 날짜) 시드: `_seed_of(seed, tag.seed_key, 4, day)`. Bad 구간 수 `poisson(bad_segments_per_tag_week / 7)`, 시작은 하루 안 균등, 길이는 `bad_duration` 균등. `Uncertain` 은 샘플마다 `uncertain_rate × (interval_sec / sample 기준 1 s)` 즉 10초 태그는 ×10. Bad 가 Uncertain 보다 우선. 하루 블록 캐시(태그당 2일).
  - `Scenario.apply` 순서: 정상+편차 → 정지 → freeze → quality 이벤트 → `random_quality` → gap. `Bad`/`ScanOff` 면 값은 None(생성기에서 처리 중).
  - `SampleGenerator.generate` 는 키로 돈다. 샘플의 `equip_id, tag_id` 는 키에서.

- [ ] **Step 1: 실패하는 테스트**

기존 테스트의 `model.normal_values("VI-P-1101A", …)` 류 호출을 키 `("P-1101A", "VI-P-1101A")` 로 바꾼다(`tests/test_rfcc_model.py`, `tests/test_signal_model.py`, `tests/test_scenario.py` 의 `sc.deviation`, `sc.apply`). 추가:

`tests/test_rfcc_model.py`:

```python
def test_new_signal_kinds(rfcc_plant_path, rfcc_runtime_path):
    rt = load_runtime(rfcc_runtime_path)
    plant = load_plant(rfcc_plant_path).model_copy(deep=True)
    tags = plant.equipment[0].tags
    tags[2].signal = "differential_pressure"      # PS-P-1101A 를 차압으로
    tags[1].signal = "process_temp"               # TI-P-1101A 를 공정 온도로
    model = RfccSignalModel(plant, rt.seed, 1)
    epochs = epochs_range(T0, T0 + timedelta(minutes=10), 10)
    assert abs(model.normal_values(("P-1101A", "PS-P-1101A"), epochs).mean() - 3.2) < 0.2
    assert abs(model.stopped_values(("P-1101A", "PS-P-1101A"), epochs).mean()) < 0.1
    assert abs(model.normal_values(("P-1101A", "TI-P-1101A"), epochs).mean() - 66) < 3
    assert abs(model.stopped_values(("P-1101A", "TI-P-1101A"), epochs).mean() - 25) < 1


def test_same_tag_name_on_two_equipment_gives_different_values(rfcc_plant_path, rfcc_runtime_path):
    rt = load_runtime(rfcc_runtime_path)
    plant = load_plant(rfcc_plant_path).model_copy(deep=True)
    plant.equipment[1].tags[2].tag_id = "TI-P-1101A"         # B-1201 의 온도 태그 이름을 P-1101A 와 같게
    model = RfccSignalModel(plant, rt.seed, 1)
    epochs = epochs_range(T0, T0 + timedelta(minutes=10), 10)
    a = model.normal_values(("P-1101A", "TI-P-1101A"), epochs)
    b = model.normal_values(("B-1201", "TI-P-1101A"), epochs)
    assert abs(a.mean() - 66) < 3 and abs(b.mean() - 78) < 3
```

`tests/test_scenario.py`:

```python
def test_random_quality_is_deterministic_and_near_rate(rfcc_plant_path, rfcc_runtime_path):
    from simulator.random_quality import RandomQualityMaker
    from common.scenario_models import RandomQuality
    plant, runtime = load_plant(rfcc_plant_path), load_runtime(rfcc_runtime_path)
    cfg = RandomQuality(uncertain_rate=0.001, bad_segments_per_tag_week=7.0, bad_duration=("30s", "400s"))
    maker = RandomQualityMaker(plant, runtime.seed, cfg)
    key = ("P-1101A", "VI-P-1101A")
    epochs = epochs_range(datetime(2026, 11, 9, tzinfo=KST), datetime(2026, 11, 16, tzinfo=KST), 1)   # 7일
    q = maker.qualities(key, epochs, 1)
    again = maker.qualities(key, epochs[:86400], 1)
    np.testing.assert_array_equal(q[:86400], again)
    bad, unc = (q == "Bad").sum(), (q == "Uncertain").sum()
    assert 2 * 30 <= bad <= 14 * 400                      # 주당 7개 구간 ± poisson
    assert 0.0004 * len(q) < unc < 0.002 * len(q)
    assert maker.qualities(("B-1201", "VX-B-1201"), epochs[:3600], 1) is None or True   # 없을 수도 있다
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_rfcc_model.py tests/test_scenario.py tests/test_signal_model.py -q`
Expected: 키 호출이 `KeyError`/`TypeError`, `random_quality` 모듈 없음.

- [ ] **Step 3: 구현**

사전·캐시를 키로 바꾸고, `RfccSignalModel._speed_source: dict[TagKey, tuple[EquipmentConfig, TagConfig]]`, 피구동 속도는 구동기 태그의 `seed_key` 로. `random_quality.py` 는 `rfcc_model._FastBlocks` 와 같은 날짜 블록 캐시 모양.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_signal_model.py tests/test_rfcc_model.py tests/test_rfcc_generator.py tests/test_scenario.py tests/test_patterns.py tests/test_sim_clock.py tests/test_sender.py -q`
Expected: 전부 통과. (`test_simulator_run.py`, `test_backfill.py`, `test_end_to_end.py` 는 수집 서버가 바뀌는 Task 3·4 뒤에 확인.)

- [ ] **Step 5: Commit**

```bash
git add simulator tests/test_signal_model.py tests/test_rfcc_model.py tests/test_rfcc_generator.py tests/test_scenario.py
git commit -m "feat: 시뮬레이터 복합키 전환, 차압·공정 온도 종류, fan, 우연한 품질 저하"
```

---

### Task 3: 수집 서버 저장·배치 — 스키마 v2, 복합키, 하한 알람, Uncertain

**Files:**
- Modify: `collector/schema.sql`, `collector/storage.py`, `collector/seed_master.py`, `collector/seed_history.py`, `collector/ingest.py`, `collector/jobs.py`
- Test: `tests/test_jobs.py`, `tests/test_collector_ingest.py`(저장 규칙 부분은 Task 4 에서 경로 바꾼 뒤), `tests/test_schema_migration.py`(교체), `tests/test_seed_history.py`

**Interfaces:**
- Produces:
  - `schema.sql`: `schema_meta(version INTEGER NOT NULL)`; `equipment_tag(equip_id, tag_id, signal, unit, location, description, normal_ref, limit_basis, warn_lo, alarm_lo, trip_lo, warn_hi, alarm_hi, trip_hi, delay_samples, interval_sec, decimals, PRIMARY KEY (equip_id, tag_id))`; `ts_raw` PK `(equip_id, tag_id, ts)`; `tag_current` PK `(equip_id, tag_id)`; `ts_10m`/`ts_1h` PK `(equip_id, tag_id, bucket_start)`; `alarm_detail.direction TEXT NOT NULL`. `ix_ts_raw_ts` 유지.
  - `SCHEMA_VERSION = 2`. `Storage.__init__`: `sqlite_master` 에 `equipment_tag` 가 있는데 `schema_meta` 가 없거나 버전 < 2 → `ConfigError(f"DB {path} was created by an older schema (v{found}); this version needs v2 (composite tag key). Move or delete the file, or pass another --db")`. 새 DB 는 스키마 생성 뒤 `INSERT INTO schema_meta VALUES (2)`. `ADDED_COLUMNS` 마이그레이션 삭제.
  - `Storage.tag_keys() -> set[TagKey]`; `store_batch(batch_id, source_id, received_at, rows)` 의 `rows: (equip_id, tag_id, ts, value, quality)`; `current(key)`; `sim_now()` 그대로.
  - `ingest.validate_samples(samples, tag_keys: set[TagKey], oldest_allowed=None)` — 키가 없으면 `reason="unknown tag {equip}/{tag}"`. `ingest(storage, batch, received_at, tag_keys, oldest_allowed=None)`.
  - `JobRunner`: 태그 사전 키 기준. `_evaluate_alarms`: 10분 `max` 로 `hi_levels()`, `min` 으로 `lo_levels()`; 열린 알람 키 `(equip_id, tag_id, limit_type, direction)`; INSERT 에 `direction`; `exceed_pct` 하한은 `(limit − actual) / abs(limit) × 100`(limit 0 이면 0). `_aggregate` 의 `bad = SUM(quality NOT IN ('Good','Uncertain'))`.
  - `seed_master`: 컬럼 이름 명시, `normal_ref`·`limit_basis`·lo 한계 저장. `_check_nullable` 삭제(v2 만 허용).
  - `seed_history`: `failure_history.equip_id` 는 `e.equip_id`(이미 채워짐).

- [ ] **Step 1: 실패하는 테스트**

`tests/test_schema_migration.py` 를 다음으로 교체:

```python
"""예전 스키마(v1, 태그 ID 단일 키) DB 파일은 열지 않고 명확히 알린다."""
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
    Storage(tmp_path / "x.db").close()       # 다시 열어도 그대로
```

`tests/test_jobs.py` 의 `_feed` 를 `(tag_id, equip_id)` → 키 순서 `(equip_id, tag_id, ts, value, quality)` 로 고치고 추가:

```python
def test_lo_limit_alarm_uses_window_min(env):
    """하한 알람: 10분 최소값이 warn_lo 아래면 Low, direction lo."""
    storage, plant, _ = env
    plant = plant.model_copy(deep=True)
    tag = plant.tag_by_key()[("C-1101", "PI-C1101")]
    tag.limits = Limits(warn_lo=40.0, alarm_lo=35.0, warn_hi=55, alarm_hi=58, trip_hi=60)
    jobs = JobRunner(storage, plant, RuntimeConfig())
    _feed(storage, "C-1101", "PI-C1101", T0, 10, 38.0)
    _feed(storage, "C-1101", "PI-C1101", T0 + timedelta(minutes=10), 1, 48.0)
    jobs.advance(storage.sim_now())
    rows = storage.query_dicts("SELECT limit_type, direction, severity, limit_value, actual_value, cleared_at FROM alarm_detail")
    assert [(r["limit_type"], r["direction"], r["severity"], r["limit_value"], r["actual_value"]) for r in rows] == [("Soft", "lo", "Low", 40.0, 38.0)]
    _feed(storage, "C-1101", "PI-C1101", T0 + timedelta(minutes=10), 10, 48.0)
    _feed(storage, "C-1101", "PI-C1101", T0 + timedelta(minutes=20), 1, 48.0)
    jobs.advance(storage.sim_now())
    assert storage.query("SELECT cleared_at FROM alarm_detail")[0][0] is not None


def test_lo_and_hi_alarms_are_independent(env):
    storage, plant, _ = env
    plant = plant.model_copy(deep=True)
    plant.tag_by_key()[("C-1101", "PI-C1101")].limits = Limits(warn_lo=40.0, warn_hi=55.0)
    jobs = JobRunner(storage, plant, RuntimeConfig())
    rows = [("C-1101", "PI-C1101", to_db(T0 + timedelta(seconds=s)), 30.0 if s < 300 else 60.0, "Good") for s in range(0, 600, 10)]
    storage.store_batch("b-both", "SIM", to_db(T0), rows)                     # 한 구간에서 하한·상한 모두 넘는다
    _feed(storage, "C-1101", "PI-C1101", T0 + timedelta(minutes=10), 1, 50.0)
    jobs.advance(storage.sim_now())
    assert sorted(r[0] for r in storage.query("SELECT direction FROM alarm_detail WHERE cleared_at IS NULL")) == ["hi", "lo"]


def test_uncertain_counts_as_value(env):
    storage, plant, jobs = env
    rows = [("C-1101", "VI-C1101", to_db(T0 + timedelta(seconds=s)), 2.0, "Uncertain" if s % 20 else "Good") for s in range(0, 600, 10)]
    storage.store_batch("b-unc", "SIM", to_db(T0), rows)
    _feed(storage, "C-1101", "VI-C1101", T0 + timedelta(minutes=10), 1, 2.0)
    jobs.advance(storage.sim_now())
    r = storage.query_dicts("SELECT avg, count, status FROM ts_10m WHERE tag_id = 'VI-C1101' AND bucket_start = ?", (to_db(T0),))[0]
    assert r["count"] == 60 and abs(r["avg"] - 2.0) < 1e-9 and r["status"] == "Good"
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_schema_migration.py tests/test_jobs.py -q`
Expected: FAIL (v1 검사 없음, `direction` 컬럼 없음, `Limits(warn_lo=…)` 는 Task 1 로 통과하지만 `_feed` 컬럼 순서 불일치 등).

- [ ] **Step 3: 구현**

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_schema_migration.py tests/test_jobs.py tests/test_seed_history.py -q`
Expected: 통과(`test_seed_history.py` 는 `create_app` 경로를 쓰면 Task 4 로 미룬다).

- [ ] **Step 5: Commit**

```bash
git add collector/schema.sql collector/storage.py collector/seed_master.py collector/seed_history.py collector/ingest.py collector/jobs.py tests/test_jobs.py tests/test_schema_migration.py tests/test_seed_history.py
git commit -m "feat: 수집 서버 스키마 v2 — 복합키, 하한 알람(direction), Uncertain 집계, 예전 DB 거부"
```

---

### Task 4: 수집 서버 — 공장 목록, 접두어 라우팅, 루트 페이지, 화면 상대 경로

**Files:**
- Create: `collector/plants.py`, `collector/plant_context.py`, `config/plants.yaml`
- Modify: `collector/app.py`, `collector/static/index.html`, `config/runtime.yaml`(`collector_url` 에 `/hds`), `config/runtime_rfcc.yaml`(`/rfcc`)
- Test: `tests/test_collector_multi.py`(새), `tests/test_collector_ingest.py`, `tests/test_collector_queries.py`, `tests/test_collector_rfcc.py`, `tests/test_end_to_end.py`, `tests/test_seed_history.py`, `tests/test_backfill.py`, `tests/test_simulator_run.py`

**Interfaces:**
- Produces (`collector/plants.py`): `PlantEntry(key: str, plant: str, runtime: str, scenario: str | None = None, db: str)`(`key` 는 `^[a-z0-9-]+$`), `PlantsConfig(plants: list[PlantEntry])`(키 고유), `load_plants(path) -> PlantsConfig`.
- Produces (`collector/plant_context.py`): `@dataclass class PlantContext: key, plant, runtime, storage, jobs, broadcaster, listing, catch_up_task=None`; `PlantContext.open(entry: PlantEntry) -> PlantContext`(설정 로드, Storage, seed_master, seed_history, JobRunner, Broadcaster, `_equipment_listing`); `close()`. `listing` 의 태그 항목에 `normal_ref`, `limit_basis`, `limits`(dict 또는 None) 포함.
- Produces (`collector/app.py`):
  - `create_app(plants: PlantsConfig) -> FastAPI`. 호환: `create_single_app(plant_path, runtime_path, db_path, scenario_path=None, key="plant") -> FastAPI`.
  - 라우터 접두어 `/{plant_key}`: `POST /ingest`, `GET /health`(기존 필드 + `key`, `plant_name`), `GET /equipment`, `GET /current`, `GET /equipment/{equip_id}/tags/{tag_id}/current`, `GET /timeseries?equip_id=&tag_id=&from=&to=&interval=`, `GET /alarms`, `GET /stream`, `GET /`(index.html). 모르는 키 → 404 `{"detail": "unknown plant key X"}`. `GET /{plant_key}`(슬래시 없음) → 307 `/{plant_key}/`.
  - 루트 `GET /health` → `{"status": "ok", "plants": {key: {plant_id, plant_name, sim_now, last_received_at, catching_up}}}`. 루트 `GET /` → 공장 목록 HTML(키·plant_id·이름·설비 수·태그 수·`/{key}/` 링크).
  - CLI: `--plants`(기본 `PLANTS_CONFIG` 또는 `config/plants.yaml`), `--host`, `--port`.
- `config/plants.yaml`: 설계 §4.7 의 여섯 항목 (cdu/hou/rhds/vrds 파일은 Task 5 에서 생긴다; 이 Task 의 테스트는 임시 목록을 쓴다).
- `index.html`: `fetch("equipment")`, `fetch("current")`, `new EventSource("stream")`, 제목은 `health` 의 `plant_name`. 한계값이 있는 태그는 값 칸에 `title` 로 한계값 요약.

- [ ] **Step 1: 실패하는 테스트** (`tests/test_collector_multi.py`)

```python
"""여러 공장을 접두어로 서빙하는 수집 서버 (설계 §4.7)."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from collector.app import create_app
from collector.plants import PlantEntry, PlantsConfig
from common.message import IngestBatch, Sample

T0 = datetime(2026, 11, 9, 1, 0, tzinfo=timezone.utc)


@pytest.fixture
def client(plant_path, runtime_path, rfcc_plant_path, rfcc_runtime_path, rfcc_scenario_path, tmp_path):
    cfg = PlantsConfig(plants=[
        PlantEntry(key="hds", plant=str(plant_path), runtime=str(runtime_path), db=str(tmp_path / "hds.db")),
        PlantEntry(key="rfcc", plant=str(rfcc_plant_path), runtime=str(rfcc_runtime_path), scenario=str(rfcc_scenario_path), db=str(tmp_path / "rfcc.db")),
    ])
    with TestClient(create_app(cfg)) as c:
        yield c


def _send(client, prefix, equip_id, tag_id, start, count, step_sec, value):
    samples = [Sample(equip_id=equip_id, tag_id=tag_id, ts=start + timedelta(seconds=i * step_sec), value=value, quality="Good") for i in range(count)]
    batch = IngestBatch(source_id="T", batch_id=f"b-{prefix}-{tag_id}-{start.timestamp()}", sent_at=start, samples=samples)
    return client.post(f"{prefix}/ingest", content=batch.model_dump_json(), headers={"content-type": "application/json"})


def test_two_plants_are_isolated(client):
    assert _send(client, "/rfcc", "P-1101A", "VI-P-1101A", T0, 3, 1, 1.45).json()["stored"] == 3
    r = _send(client, "/hds", "P-1101A", "VI-P-1101A", T0, 1, 1, 1.45).json()      # hds 에는 없는 키
    assert r["stored"] == 0 and "unknown tag P-1101A/VI-P-1101A" in r["reasons"][0]["reason"]
    assert client.get("/rfcc/equipment/P-1101A/tags/VI-P-1101A/current").json()["status"] == "Good"
    assert client.get("/hds/equipment/P-1101A/tags/VI-P-1101A/current").status_code == 404
    assert client.get("/hds/health").json()["sim_now"] is None and client.get("/rfcc/health").json()["sim_now"] is not None


def test_root_health_and_index(client):
    h = client.get("/health").json()
    assert set(h["plants"]) == {"hds", "rfcc"} and h["plants"]["rfcc"]["plant_id"] == "PL-RFCC"
    index = client.get("/")
    assert index.status_code == 200 and '/rfcc/' in index.text and 'PL-HDS' in index.text
    assert client.get("/rfcc", follow_redirects=False).status_code == 307
    assert client.get("/nope/health").status_code == 404
    page = client.get("/rfcc/").text
    assert 'fetch("equipment")' in page and 'EventSource("stream")' in page


def test_equipment_listing_carries_master_fields(client):
    tags = {(e["equip_id"], t["tag_id"]): t for e in client.get("/hds/equipment").json() for t in e["tags"]}
    t = tags[("C-1101", "VI-C1101")]
    assert t["limits"] == {"warn_hi": 4.5, "alarm_hi": 6.0, "trip_hi": 7.1} and "normal_ref" in t and "limit_basis" in t
```

기존 수집 서버 테스트는 `create_app(plant_path, runtime_path, db)` → `create_single_app(...)` 로, 경로는 `/plant/...` 접두어와 새 경로(`/plant/equipment/{e}/tags/{t}/current`, `/plant/timeseries?equip_id=&tag_id=`)로 바꾼다. `test_end_to_end.py`·`test_backfill.py`·`test_simulator_run.py` 가 앱에 보낼 때는 `HttpSender("http://testserver/plant", transport=...)`.

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_collector_multi.py -q`
Expected: `collector.plants` 없음.

- [ ] **Step 3: 구현**

`app.py` 는 라우트 정의와 CLI 만 남기고, 컨텍스트 생성·목록은 `plant_context.py` 로. 의존성: `def context(plant_key: str, request: Request) -> PlantContext` → `request.app.state.plants.get(plant_key)` 없으면 404.

- [ ] **Step 4: 통과 확인 — 전체**

Run: `uv run pytest -q`
Expected: 전부 통과 (115 + 새 테스트).

- [ ] **Step 5: Commit**

```bash
git add collector config/plants.yaml config/runtime.yaml config/runtime_rfcc.yaml tests
git commit -m "feat: 수집 서버 다중 공장 — plants.yaml, /{공장} 접두어 라우팅, 루트 목록·상태, 복합키 조회 API"
```

---

### Task 5: 네 공장 구성안·실행·시나리오 파일

**Files:**
- Create: `config/plant_{cdu,hou,rhds,vrds}.yaml`, `config/runtime_{cdu,hou,rhds,vrds}.yaml`, `config/scenario_{cdu,hou,rhds,vrds}.yaml`
- Modify: `tests/conftest.py`(픽스처 `plant_cfg(key)` 같은 도우미 또는 공장별 네 픽스처), `config/plants.yaml`(네 항목 확정)
- Test: `tests/test_multi_plant_configs.py`

**Interfaces:**
- Consumes: 설계 §4.4 대응표, §4.5 생성 규칙, §4.6 이벤트 표. 추출본 `scratchpad/masters.json`(hier, 설비 태그 행, 첫 6시간 통계: mean, sd, diff_sd, decimals, interval).
- Produces: 네 공장 yaml. `equipment_types`: `{PM 펌프 P pump}`, `{CC 압축기 C compressor}`, `{FN 팬 F fan}`. `signals`: 그 공장에 나오는 종류만(대표 접두어·단위는 첫 태그의 것). 설비 `seed_key: {key}-e01…`, 태그 `seed_key: {key}-r001…`. `decimals` 는 추출본의 값. `noise_sd = diff_sd / c`(기본 0.58, 온도 0.49, 속도 0.60, 축 위치 0.40), 소수 6자리.
- 시나리오 파일은 §4.6 표 그대로. 모든 이벤트 `repeat_every: 7d`, `failure_id` 없음. HOU 에 `random_quality: {uncertain_rate: 0.00003, bad_segments_per_tag_week: 1.0, bad_duration: [30s, 400s]}`.
- 실행 설정: `runtime_rfcc.yaml` 복사, `source_id: SIM-{KEY}`, `seed`(CDU 20261101, HOU 20261109, RHDS 20261110, VRDS 20261111), `collector_url: http://localhost:8000/{key}`.

- [ ] **Step 1: 실패하는 테스트** (`tests/test_multi_plant_configs.py`)

```python
"""CDU · HOU · RHDS · VRDS 구성안이 자료의 마스터 표와 같다 (설계 §5.1)."""
import pytest

from common.config_models import load_plant, load_runtime
from common.scenario_models import load_scenario

EXPECTED = {"cdu": ("PL-CDU", 7, 46), "hou": ("PL-HOU", 7, 61), "rhds": ("PL-RHDS", 6, 43), "vrds": ("PL-VRDS", 6, 43)}


@pytest.mark.parametrize("key", list(EXPECTED))
def test_plant_counts_and_files_load(key, config_dir):
    plant = load_plant(config_dir / f"plant_{key}.yaml")
    plant_id, n_equip, n_tags = EXPECTED[key]
    assert plant.plant.plant_id == plant_id and plant.plant.signal_model == "rfcc"
    assert len(plant.equipment) == n_equip and len(plant.tag_keys()) == n_tags
    rt = load_runtime(config_dir / f"runtime_{key}.yaml")
    assert rt.collector_url.endswith(f"/{key}") and rt.sample_interval_sec == 1
    rt.validate_tag_intervals(plant)
    load_scenario(config_dir / f"scenario_{key}.yaml", plant)


def test_master_values_match_excel_samples(config_dir):
    cdu = load_plant(config_dir / "plant_cdu.yaml").tag_by_key()
    t = cdu[("P-8101A", "PI-8101A")]
    assert (t.limits.warn_lo, t.limits.alarm_lo, t.limits.warn_hi) == (2.02, 1.62, None)
    assert t.unit == "kg/cm2(g)" and t.normal_ref == "2.38~3.22" and t.interval_sec == 1 and abs(t.normal.base - 2.79) < 0.05
    hou = load_plant(config_dir / "plant_hou.yaml").tag_by_key()
    z = hou[("C-2301", "ZI-2301")]
    assert (z.limits.warn_hi, z.limits.alarm_lo, z.limits.alarm_hi, z.limits.trip_lo, z.limits.trip_hi) == (0.2, -0.3, 0.3, -0.5, 0.5)
    assert hou[("C-2301", "TI-2301A")].interval_sec == 10 and hou[("C-2301", "TI-2301A")].description == "구동측 저널 베어링 패드 온도"
    rhds = load_plant(config_dir / "plant_rhds.yaml")
    assert [k for k in rhds.tag_keys() if k[1] == "TI-B-3101"] == [("P-3101", "TI-B-3101"), ("C-3101", "TI-B-3101"), ("F-3101", "TI-B-3101")]
    s = rhds.tag_by_key()[("C-3101", "SI-3101")]
    assert (s.limits.warn_lo, s.limits.warn_hi) == (8500, 10000) and s.signal == "speed"
    assert rhds.tag_by_key()[("F-3101", "PDI-3101")].signal == "differential_pressure"
    assert rhds.tag_by_key()[("P-3102", "TI-F-3102")].signal == "process_temp"
    vrds = load_plant(config_dir / "plant_vrds.yaml")
    assert load_runtime(config_dir / "runtime_vrds.yaml").seed == 20261111
    assert {e.seed_key for e in vrds.equipment}.isdisjoint({e.seed_key for e in rhds.equipment})


def test_plants_yaml_lists_six(config_dir):
    from collector.plants import load_plants
    assert [p.key for p in load_plants(config_dir / "plants.yaml").plants] == ["hds", "rfcc", "cdu", "hou", "rhds", "vrds"]
```

`conftest.py` 에 `config_dir` 픽스처(`CONFIG_DIR`) 추가.

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_multi_plant_configs.py -q`
Expected: 파일 없음.

- [ ] **Step 3: 생성 스크립트 작성·실행 (스크래치)**

`masters.json` → 네 yaml. 순서: Unit ID 번호순, 설비는 그 안에서 ID 순. 머리 주석: 출처 폴더·시트, 생성 규칙 요약, "한계값·정상 기준·근거는 자료 그대로". 시나리오·실행 설정은 손으로 쓴다(§4.6, §4.5).

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_multi_plant_configs.py tests/test_collector_multi.py -q`
Expected: 통과. 추가로 `uv run python -c` 로 네 공장 `SampleGenerator` 를 만들어 1초 구간을 생성해 보고 값이 모두 finite 인지 확인한다.

- [ ] **Step 5: Commit**

```bash
git add config tests/conftest.py tests/test_multi_plant_configs.py
git commit -m "feat: CDU·HOU·RHDS·VRDS 구성안·실행 설정·시나리오 — Excel 마스터 표 그대로"
```

---

### Task 6: 실행 구성·문서·실제 실행 확인

**Files:**
- Modify: `docker-compose.yml`, `.gitignore`, `README.md`, `pyproject.toml`(description)
- Modify: `.superpowers` 없음

- [ ] **Step 1: compose·gitignore**

`collector` 서비스: `python -m collector.app --plants config/plants.yaml --port 8000`, healthcheck 루트 `/health`. 시뮬레이터 여섯 개(`sim-hds`, `sim-rfcc`, `sim-cdu`, `sim-hou`, `sim-rhds`, `sim-vrds`): `--plant config/plant_X.yaml --runtime config/runtime_X.yaml --scenario config/scenario_X.yaml --time-mode ${SIM_TIME_MODE:-backfill}`, `COLLECTOR_URL: http://collector:8000/{key}`. `.gitignore` 에 세 샘플 폴더.

- [ ] **Step 2: README**

여섯 공장 표(키·plant_id·설비·태그·주기·근거 폴더), 빠른 시작(수집 서버 `--plants`, 시뮬레이터 공장별, compose), 접두어 API 표(복합키 경로), 설정 파일 표(네 공장 추가, `plants.yaml`), 구성안 필드(한계값 상·하한, `normal_ref`·`limit_basis`, `TagRef`), 시나리오(`equip_id` 필수·`related.equip_id`·`random_quality`), 품질 `Uncertain`, DB 절(스키마 v2·예전 파일 안내·용량 약 4 GB/일·단일 프로세스 백필 직렬화), 폴더 구조.

- [ ] **Step 3: 로컬 실제 실행**

수집 서버(`--plants`)와 시뮬레이터 두 개(cdu, rhds, `--time-mode live`)를 띄워 `/health` 에 여섯 공장, `/cdu/current` 46행 Good, `/rhds/current` 43행(TI-B-3101 세 행) 확인. `/rhds/` 를 브라우저로 열어 1초 갱신 확인. 종료.

- [ ] **Step 4: Docker Compose**

기존 `data/rfcc.db`(v1) 는 `data/old/` 로 옮긴다(삭제하지 않는다). `docker compose up -d --build` → 루트 `/health` 여섯 공장, 백필이 끝나면 `/`, `/cdu/`, `/hou/` 값 갱신, `du -sh data` 를 README 에 기록. 백필 소요 시간도 적는다.

- [ ] **Step 5: 전체 테스트·Commit**

```bash
uv run pytest -q
git add docker-compose.yml .gitignore README.md pyproject.toml
git commit -m "docs: 여섯 공장 실행 구성 — compose, plants.yaml, 접두어 API, 복합키·한계값·용량"
```
