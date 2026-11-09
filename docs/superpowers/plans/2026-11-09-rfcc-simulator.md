# RFCC 구성안 시뮬레이터 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** RFCC 샘플 자료(설비 12대·태그 81개, 온도 10초·나머지 1초)의 신호 모델을 시뮬레이터에 옮겨 끝없이 생성하고, 수집 서버가 서빙하는 페이지에서 1초마다 갱신되는 값을 본다.

**Architecture:** 구성안 yaml 의 `plant.signal_model` 로 신호 모델(`hds` / `rfcc`)을 고른다. `RfccSignalModel` 은 기존 `SignalModel` 의 하위 클래스로 `normal_values` / `stopped_values` 만 바꾸며, 느린 성분은 고정 기준 시각부터의 600초 매듭 OU 보간, 빠른 성분은 날짜 블록의 잘린 지수 커널 합성곱으로 만들어 모든 값을 측정 시각의 순수 함수로 유지한다. 수집 서버는 보관 삭제를 10분 배치로 옮기고 `/equipment`, `/current`, `/`(정적 HTML) 를 더한다.

**Tech Stack:** Python 3.11, NumPy, Pydantic v2, FastAPI, SQLite, 의존성 없는 HTML+JS. 새 패키지 추가 없음.

**Spec:** `docs/superpowers/specs/2026-11-09-rfcc-simulator-design.md` (기획서 `plant-simulator-plan.md` 의 확장)

## Global Constraints

- 기존 의존성만 쓴다. scipy·pandas 추가 금지.
- 코드는 설비·태그 ID 형식을 해석하지 않는다. 신호 종류(`signal`)와 설비 유형 `kind` 는 유형이므로 코드가 알아도 된다.
- 모든 생성값은 `(seed, seed_key, 측정 시각)` 의 순수 함수. 어느 구간으로 잘라 요청해도 같은 값.
- 느린 성분 격자 600초, 커널 길이 5τ, 블록 캐시 태그당 최근 2일, 고정 기준 시각 KST 2026-01-01 00:00.
- 알람 한계값을 만들지 않는다. RFCC 태그는 `limits` 없음.
- 메시지 형식 v1, `/ingest` 규칙, 배치의 측정 시각 기준은 바꾸지 않는다.
- 기존 plant_hds 테스트 80개는 계속 통과해야 한다(두 개는 설계가 바꾼 동작에 맞춰 고친다: `test_all_seven_patterns_registered`, `test_daily_retention_deletes_old_raw`).
- 코드 스타일: 모듈 머리 한 문단 한국어 docstring, 함수·데이터에 짧은 주석, 파일은 책임별로 나눈다.
- 커밋 메시지는 한국어, 끝에 `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Review Focus

1. 1초 주기 1시간 백필 묶음(약 24만 샘플, JSON 30 MB)이 전송 타임아웃 30초 안에 처리돼야 한다 → Task 4 생성 시간 테스트, Task 9 실제 전송 확인.
2. 기준 시각(2026-01-01 KST) 이전 epoch 를 요청하면 조용히 쓰레기 값을 내지 말고 `ConfigError` → Task 3 테스트.
3. 10초 태그가 1초 전송에서 NoData 로 깜빡이면 안 된다 → Task 6 테스트.
4. 예전 DB 의 `job_state` 에 `daily` 행이 남아 있어도 수집 서버가 그대로 돌아야 한다 → Task 6 테스트.
5. 자정·재시작에서 값이 튀지 않아야 한다 → Task 3 자정 연속성·구간 분할 불변 테스트.

---

### Task 1: 구성안 형식 확장과 DB 스키마

**Files:**
- Modify: `common/config_models.py`
- Modify: `collector/schema.sql` (`equipment`, `equipment_tag`)
- Modify: `collector/seed_master.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `PlantMeta.signal_model: Literal["hds", "rfcc"] = "hds"`
  - `EquipmentType.kind: str | None = None` (pump / blower / expander / compressor / turbine)
  - `EquipmentConfig.grade: int | None = None`, `EquipmentConfig.seed_key: str | None = None`
  - `TagConfig.limits: Limits | None = None`, `TagConfig.location: str | None = None`, `TagConfig.description: str | None = None`, `TagConfig.interval_sec: int | None = None`, `TagConfig.decimals: int = 3`, `TagConfig.delay_samples: int = 1`
  - `NormalModel.daily_amp: float = 0.0`, `NormalModel.drift_sd: float = 0.0`
  - `PlantConfig.tag_interval_sec(tag_id: str, default: int) -> int` — 태그의 `interval_sec` 또는 기본값
  - `PlantConfig.equipment_kind(equip_id: str) -> str | None` — 설비 유형의 `kind`
  - 검증: `signal_model == "rfcc"` 이면 모든 설비에 `seed_key` 필수(없으면 `ConfigError("equipment X: seed_key is required for signal_model rfcc")`), 설비 `seed_key` 고유.
  - `RuntimeConfig.validate_tag_intervals(plant: PlantConfig) -> None` — 각 태그 `interval_sec` 가 `sample_interval_sec` 의 배수이고 86400 을 나누지 않으면 `ConfigError`.
  - `equipment_tag` 컬럼: `location TEXT`, `description TEXT`, `warn_hi REAL`, `alarm_hi REAL`, `trip_hi REAL` (NULL 허용), `interval_sec INTEGER`(NULL 허용), `decimals INTEGER NOT NULL`. `equipment.grade INTEGER` NULL 허용.

- [ ] **Step 1: 실패하는 테스트 작성** (`tests/test_config.py` 에 추가)

```python
def _rfcc_like(seed_key_on_equipment=True):
    """최소 rfcc 구성안 dict."""
    equip = {"equip_id": "P-1", "name": "pump", "type": "PM", "unit_id": "U-1",
             "tags": [{"tag_id": "TI-P-1", "signal": "bearing_temp", "unit": "degC", "seed_key": "r1",
                       "description": "베어링 온도", "interval_sec": 10, "decimals": 4,
                       "normal": {"base": 66, "noise_sd": 0.035}}]}
    if seed_key_on_equipment:
        equip["seed_key"] = "e1"
    return {"plant": {"plant_id": "PL", "name": "p", "config_version": 1, "signal_model": "rfcc",
                      "unit_groups": [{"id": "UG-1", "name": "g", "units": [{"id": "U-1", "name": "u"}]}]},
            "equipment_types": [{"type": "PM", "name": "펌프", "prefix": "P", "kind": "pump"}],
            "signals": [{"signal": "bearing_temp", "prefix": "TI", "unit": "degC", "name": "베어링 온도"}],
            "equipment": [equip]}


def test_rfcc_fields_are_optional_or_new():
    plant = PlantConfig.model_validate(_rfcc_like())
    tag = plant.all_tags()[0]
    assert plant.plant.signal_model == "rfcc"
    assert tag.limits is None and tag.location is None and tag.description == "베어링 온도"
    assert tag.decimals == 4 and plant.tag_interval_sec("TI-P-1", 1) == 10
    assert plant.equipment[0].grade is None and plant.equipment_kind("P-1") == "pump"
    assert tag.normal.daily_amp == 0.0 and tag.normal.drift_sd == 0.0


def test_rfcc_requires_equipment_seed_key():
    with pytest.raises(ConfigError, match="equipment P-1: seed_key is required"):
        PlantConfig.model_validate(_rfcc_like(seed_key_on_equipment=False))


def test_tag_interval_must_be_multiple_of_sample_interval():
    plant = PlantConfig.model_validate(_rfcc_like())
    RuntimeConfig(sample_interval_sec=1, send_interval_sec=1).validate_tag_intervals(plant)       # 10 은 1 의 배수
    with pytest.raises(ConfigError, match="TI-P-1"):
        RuntimeConfig(sample_interval_sec=3, send_interval_sec=3).validate_tag_intervals(plant)   # 10 은 3 의 배수가 아니다


def test_hds_plant_still_loads_with_defaults(plant_path):
    plant = load_plant(plant_path)
    assert plant.plant.signal_model == "hds"
    assert all(t.limits is not None and t.decimals == 3 for t in plant.all_tags())
    assert plant.tag_interval_sec("VI-C1101", 10) == 10
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_config.py -q`
Expected: FAIL — `signal_model` extra forbidden / `tag_interval_sec` 없음 등 4개 실패.

- [ ] **Step 3: `common/config_models.py` 구현**

위 Interfaces 의 필드·기본값·메서드를 추가한다. `_cross_check` 에 rfcc 설비 `seed_key` 필수·고유 검사를 넣는다. `limits` 가 None 인 태그에서 기존 코드(`random_anomalies`, `_evaluate_alarms`, `seed_master`)가 깨지는 건 Task 5·6 에서 다룬다.

- [ ] **Step 4: `collector/schema.sql`, `collector/seed_master.py` 수정**

`equipment_tag` 를 Interfaces 대로 바꾸고(컬럼 순서: tag_id, equip_id, signal, unit, location, description, warn_hi, alarm_hi, trip_hi, delay_samples, interval_sec, decimals), `seed_master` 가 `limits` None 이면 NULL 을 넣게 한다. `Storage.tag_owner()` 는 그대로.

- [ ] **Step 5: 통과 확인과 전체 회귀**

Run: `uv run pytest -q`
Expected: 84 passed (80 + 4).

- [ ] **Step 6: Commit**

```bash
git add common/config_models.py collector/schema.sql collector/seed_master.py tests/test_config.py
git commit -m "feat: 구성안 형식 확장 — signal_model, 태그별 주기·자릿수·설명, 선택적 한계값, 설비 seed_key"
```

---

### Task 2: RFCC 구성안·실행 설정 파일

**Files:**
- Create: `config/plant_rfcc.yaml`, `config/runtime_rfcc.yaml`
- Modify: `tests/conftest.py` (픽스처 `rfcc_plant_path`, `rfcc_runtime_path`, `rfcc_scenario_path`)
- Test: `tests/test_rfcc_config.py`

**Interfaces:**
- Consumes: Task 1 필드.
- Produces: `config/plant_rfcc.yaml` — `plant_id: PL-RFCC`, `signal_model: rfcc`, unit_groups 5 (`UG-RFCC-01`~`05`, 이름: 원료 공급 / 반응·재생 / 주분별 / 가스 압축·회수 / 제품 처리), units 8 (`UNIT-RFCC-01`~`08`; 이름: 원료 공급 / 촉매 재생 / 동력 회수 / 슬러리·LCO 순환·탑정 처리 / 습가스 압축 / 습가스 압축 구동 / 흡수·분별·안정화 / LPG·가솔린 처리), `equipment_types`: `{PM 펌프 P pump}`, `{BL 송풍기 B blower}`, `{EX 팽창 터빈 E expander}`, `{CC 원심 압축기 C compressor}`, `{ST 증기 터빈 T turbine}`, `signals` 14종(설계 §7 표의 이름·대표 접두어·대표 단위), 설비 12대(`seed_key: e01`~`e12`, `driven_by`: B-1201→E-1202, C-1401→T-1401, `aliases: []`), 태그 81개(`seed_key: r001`~`r081`, `description`, `unit`, `interval_sec` 1 또는 10, `decimals` 4(ZI 는 5), `normal: {base, noise_sd}`). 설비·태그 순서와 수치는 `rfcc-20261109/support/config.json` 그대로.
- `config/runtime_rfcc.yaml`: `units: [all]`, `source_id: SIM-RFCC`, `sample_interval_sec: 1`, `send_interval_sec: 1`, `time_mode: live`, `speed: 60`, `backfill_days: 1`, `seed: 61109`, `collector_url: http://localhost:8000`, `raw_retention_days: 1`.

- [ ] **Step 1: 실패하는 테스트** (`tests/test_rfcc_config.py`)

```python
"""RFCC 구성안 파일 (설계 §4.1, §7)."""
from common.config_models import load_plant, load_runtime

TEMPS = {"bearing_temp", "suction_temp", "discharge_temp", "oil_temp"}


def test_rfcc_plant_matches_sample_package(rfcc_plant_path):
    plant = load_plant(rfcc_plant_path)
    tags = plant.all_tags()
    assert plant.plant.signal_model == "rfcc"
    assert len(plant.equipment) == 12 and len(tags) == 81
    assert len(plant.unit_ids()) == 8 and len(plant.plant.unit_groups) == 5
    temps = [t for t in tags if t.signal in TEMPS]
    assert len(temps) == 16 and all(t.interval_sec == 10 for t in temps)
    assert all(t.interval_sec == 1 for t in tags if t.signal not in TEMPS)
    assert all(t.limits is None for t in tags)
    by_id = plant.tag_by_id()
    assert by_id["VI-P-1101A"].normal.base == 1.45 and by_id["VI-P-1101A"].normal.noise_sd == 0.03375
    assert by_id["ZI-E-1202"].decimals == 5 and by_id["FI-B-1201"].decimals == 4
    equip = plant.equipment_by_id()
    assert equip["B-1201"].driven_by == "E-1202" and equip["C-1401"].driven_by == "T-1401"
    assert plant.equipment_kind("E-1202") == "expander" and plant.equipment_kind("P-1101A") == "pump"


def test_rfcc_runtime(rfcc_runtime_path, rfcc_plant_path):
    rt = load_runtime(rfcc_runtime_path)
    assert (rt.sample_interval_sec, rt.send_interval_sec, rt.backfill_days, rt.raw_retention_days, rt.seed) == (1, 1, 1, 1, 61109)
    rt.validate_tag_intervals(load_plant(rfcc_plant_path))
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_rfcc_config.py -q`
Expected: FAIL — fixture `rfcc_plant_path` 없음.

- [ ] **Step 3: yaml 두 개 작성**

`plant_rfcc.yaml` 은 손으로 쓰지 말고 일회용 스크립트(스크래치 폴더, 커밋하지 않음)로 `support/config.json` 을 읽어 설계 §7 표대로 변환해 쓴다. 머리 주석에 출처(`rfcc-20261109` v1.2.0)와 "한계값 없음, 매뉴얼이 경보값 작성을 금함"을 적는다. 설비마다 `# ---- UNIT-RFCC-0n 이름 ----` 구분 주석. 태그 한 줄 형식은 plant_hds 와 같은 flow 스타일.

- [ ] **Step 4: conftest 픽스처 추가 후 통과 확인**

Run: `uv run pytest tests/test_rfcc_config.py -q`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add config/plant_rfcc.yaml config/runtime_rfcc.yaml tests/conftest.py tests/test_rfcc_config.py
git commit -m "feat: RFCC 구성안·실행 설정 — 설비 12대, 태그 81개, 온도 10초·나머지 1초"
```

---

### Task 3: RFCC 신호 모델

**Files:**
- Create: `simulator/rfcc_model.py`
- Test: `tests/test_rfcc_model.py`

**Interfaces:**
- Consumes: `SignalModel`, `_seed_of`, `KST`, `DAY_SEC` (`simulator/signal_model.py`); Task 1 필드.
- Produces (`simulator/rfcc_model.py`):
  - `RFCC_EPOCH: float` — KST 2026-01-01 00:00 의 epoch 초. `KNOT_SEC = 600`. `KERNEL_TAUS = 5`. `BLOCK_CACHE_DAYS = 2`. `AMBIENT_TEMP_C = 25.0`.
  - `SIGNAL_KINDS: frozenset[str]` = {vibration, shaft_vibration, bearing_temp, suction_temp, discharge_temp, oil_temp, flow, suction_pressure, discharge_pressure, current, speed, axial_position, oil_pressure, steam_pressure}
  - `ar1_series(rng: np.random.Generator, sd: float, tau_sec: float, step_sec: float, n: int) -> np.ndarray` — 정상 AR(1) n 개. 잘린 지수 커널(길이 `ceil(KERNEL_TAUS·tau/step)`, 합이 분산 sd² 가 되도록 정규화)과 `n+K−1` 개 정규 잡음의 `np.convolve(..., "valid")`.
  - `lowpass(forcing: np.ndarray, tau_sec: float, step_sec: float) -> np.ndarray` — 1차 지연, DC 이득 1, 커널 길이 5τ. 입력 앞 K−1 개는 예열용이며 출력 길이는 `len(forcing) − K + 1`.
  - `class RfccSignalModel(SignalModel)`: `__init__(plant, seed, sample_interval_sec)`; `normal_values(tag_id, epochs) -> np.ndarray`; `stopped_values(tag_id, epochs) -> np.ndarray`. 모르는 `signal` → `ConfigError("tag X: unknown signal kind Y for signal_model rfcc")`. `driven_by` 양쪽에 `speed` 태그가 있는데 `base` 가 다르면 `ConfigError`. `epochs.min() < RFCC_EPOCH` → `ConfigError`.
  - 피구동 설비의 `speed` 태그는 구동기 `speed` 태그의 시드(그 태그의 `seed_key`)로 같은 값을 만든다. `speed` 의 부하 계수 k: 설비 `kind` 가 blower/expander 면 0.12, 아니면 0.25.
  - 느린 성분 키(상수): 공정 부하 OU `("load",)`, 부하 변경 매개변수 `("load_step", day)`, 외기 OU `("ambient",)`, 설비 부하 `(equip.seed_key,)`, 진동 느린 성분 `(tag.seed_key, "slow")`. 모두 `_seed_of(seed, "|".join(키), 성분 번호)` 로 시드.
  - 부하 변경(하루 한 번): 날짜 d 마다 `rng = _seed_of(seed, "load_step", d)` 에서 시작 시각 `t0 = d·86400 + U(0, 86400)`(KST 하루 기준 epoch), 방향 `±1`, 길이 `U(4h, 10h)`. 기여 `0.018·dir·(tanh((t−t0)/2000) − tanh((t−t0−len)/2000))/2`. 어느 epoch 든 날짜 d−1, d, d+1 세 개를 더한다.

- [ ] **Step 1: 실패하는 테스트** (`tests/test_rfcc_model.py`)

```python
"""RFCC 신호 모델 (설계 §4.2): 순수 함수, 자정 연속, 축 속도 공유, 정지 상태."""
from datetime import datetime, timedelta

import numpy as np
import pytest

from common.config_models import ConfigError, load_plant, load_runtime
from common.sim_clock import KST
from simulator.rfcc_model import RFCC_EPOCH, RfccSignalModel, ar1_series, lowpass
from simulator.signal_model import epochs_range

T0 = datetime(2026, 11, 9, 0, 0, tzinfo=KST)


@pytest.fixture(scope="module")
def model(rfcc_plant_path, rfcc_runtime_path):
    rt = load_runtime(rfcc_runtime_path)
    return RfccSignalModel(load_plant(rfcc_plant_path), rt.seed, rt.sample_interval_sec)


def test_ar1_series_is_stationary_with_requested_sd():
    x = ar1_series(np.random.default_rng(1), sd=2.0, tau_sec=6, step_sec=1, n=200_000)
    assert abs(x.std() - 2.0) < 0.05
    a = np.exp(-1 / 6)
    assert abs(np.corrcoef(x[:-1], x[1:])[0, 1] - a) < 0.01


def test_ar1_prefix_is_stable_when_longer_series_requested():
    short = ar1_series(np.random.default_rng(7), 1.0, 1400, 600, 100)
    long = ar1_series(np.random.default_rng(7), 1.0, 1400, 600, 300)
    np.testing.assert_allclose(short, long[:100])


def test_lowpass_has_unit_dc_gain():
    out = lowpass(np.full(2000, 3.0), tau_sec=240, step_sec=10)
    assert np.allclose(out, 3.0, atol=1e-6)


def test_values_do_not_depend_on_chunking(model):
    whole = epochs_range(T0, T0 + timedelta(hours=2), 1)
    for tag in ("VI-P-1101A", "TI-P-1101A", "SI-C-1401", "FI-B-1201"):
        one = model.normal_values(tag, whole)
        two = np.concatenate([model.normal_values(tag, whole[:3600]), model.normal_values(tag, whole[3600:])])
        single = np.array([model.normal_values(tag, whole[i:i + 1])[0] for i in (0, 1234, 7199)])
        np.testing.assert_allclose(one, two)
        np.testing.assert_allclose(one[[0, 1234, 7199]], single)


def test_midnight_is_continuous(model):
    plant = model.plant
    for tag_id in ("VI-P-1101A", "FI-P-1101A", "TI-P-1101A", "SI-B-1201"):
        tag = plant.tag_by_id()[tag_id]
        step = tag.interval_sec or 1
        before = model.normal_values(tag_id, np.array([int(T0.timestamp()) - step]))[0]
        after = model.normal_values(tag_id, np.array([int(T0.timestamp())]))[0]
        assert abs(after - before) < 10 * tag.normal.noise_sd + 1e-9, tag_id


def test_driven_equipment_shares_driver_speed(model):
    epochs = epochs_range(T0, T0 + timedelta(minutes=5), 1)
    np.testing.assert_allclose(model.normal_values("SI-B-1201", epochs), model.normal_values("SI-E-1202", epochs))
    np.testing.assert_allclose(model.normal_values("SI-C-1401", epochs), model.normal_values("SI-T-1401", epochs))
    assert not np.allclose(model.normal_values("SI-B-1201", epochs), model.normal_values("SI-C-1401", epochs))


def test_values_stay_near_baseline_over_a_day(model):
    epochs = epochs_range(T0, T0 + timedelta(days=1), 10)
    by_id = model.plant.tag_by_id()
    for tag_id, tol in (("VI-P-1101A", 0.25), ("TI-P-1101A", 3.0), ("PS-P-1101A", 0.1), ("FI-P-1101A", 15), ("II-P-1101A", 10)):
        v = model.normal_values(tag_id, epochs)
        assert abs(v.mean() - by_id[tag_id].normal.base) < tol, tag_id
        assert np.isfinite(v).all()


def test_stopped_values(model):
    epochs = epochs_range(T0, T0 + timedelta(minutes=1), 1)
    assert abs(model.stopped_values("VI-P-1101A", epochs).mean()) < 0.1
    assert abs(model.stopped_values("TI-P-1101A", epochs).mean() - 25.0) < 1.0
    assert abs(model.stopped_values("ZI-E-1202", epochs).mean() - 0.04) < 0.01
    assert abs(model.stopped_values("SI-C-1401", epochs).mean()) < 1.0


def test_epoch_before_fixed_origin_is_rejected(model):
    with pytest.raises(ConfigError, match="2026-01-01"):
        model.normal_values("VI-P-1101A", np.array([int(RFCC_EPOCH) - 1]))


def test_unknown_signal_kind_and_speed_mismatch_are_reported(rfcc_plant_path, rfcc_runtime_path):
    rt = load_runtime(rfcc_runtime_path)
    plant = load_plant(rfcc_plant_path)
    bad = plant.model_copy(deep=True)
    bad.equipment[0].tags[0].signal = "humidity"
    with pytest.raises(ConfigError, match="unknown signal kind humidity"):
        RfccSignalModel(bad, rt.seed, rt.sample_interval_sec)
    bad = plant.model_copy(deep=True)
    bad.tag_by_id()["SI-B-1201"].normal.base = 5300
    with pytest.raises(ConfigError, match="B-1201"):
        RfccSignalModel(bad, rt.seed, rt.sample_interval_sec)
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_rfcc_model.py -q`
Expected: FAIL — `simulator.rfcc_model` 없음.

- [ ] **Step 3: `simulator/rfcc_model.py` 구현**

설계 §4.2 의 식을 그대로 옮긴다. 구조 제안(작업자 재량):
- `_SlowProcess`: 키·sd·τ 를 받아 `RFCC_EPOCH` 부터 600초 매듭을 `ar1_series` 로 만들고 `np.interp` 로 평가. 매듭이 모자라면 같은 rng 시드로 더 긴 길이를 다시 만든다(접두 불변).
- `_FastBlocks`: `(tag_id, day)` → 블록 캐시(최근 2일). 블록은 그 태그 `interval_sec` 격자, 하루 길이, `ar1_series` + 흰 잡음.
- `RfccSignalModel.normal_values` 가 `signal` 종류별 식 함수를 사전으로 고른다. 온도는 강제항을 10초 격자(블록 앞 1200초 여분 포함)로 평가해 `lowpass` 뒤 `np.interp` 로 요청 epoch 에 맞춘다.
- `stopped_values`: 온도 25 °C, `axial_position` 은 base, 나머지 0, 거기에 빠른 잡음 × 0.3.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_rfcc_model.py -q`
Expected: 10 passed. `test_values_do_not_depend_on_chunking` 포함 전체가 10초 안에 끝나야 한다(느리면 블록 캐시가 안 되는 것).

- [ ] **Step 5: Commit**

```bash
git add simulator/rfcc_model.py tests/test_rfcc_model.py
git commit -m "feat: RFCC 신호 모델 — 공정 부하·외기·AR 잡음·온도 지연·축 속도 공유를 측정 시각의 순수 함수로"
```

---

### Task 4: 생성기 — 모델 선택, 태그별 주기, 자릿수

**Files:**
- Modify: `simulator/generator.py`
- Modify: `simulator/scenario.py:118-119` (`model_for_tests` 가 팩터리를 쓰도록)
- Test: `tests/test_rfcc_generator.py`

**Interfaces:**
- Consumes: `RfccSignalModel` (Task 3), `PlantConfig.tag_interval_sec`, `TagConfig.decimals` (Task 1).
- Produces (`simulator/generator.py`):
  - `make_signal_model(plant: PlantConfig, seed: int, sample_interval_sec: int) -> SignalModel` — `plant.plant.signal_model` 이 `rfcc` 면 `RfccSignalModel`, 아니면 `SignalModel`.
  - `SampleGenerator.__init__` 가 `runtime.validate_tag_intervals(plant)` 를 부른다.
  - `SampleGenerator.generate(start, end)`: 태그마다 `epoch % interval_sec == 0` 인 epoch 만 샘플로 만들고 `round(value, tag.decimals)`.

- [ ] **Step 1: 실패하는 테스트** (`tests/test_rfcc_generator.py`)

```python
"""태그별 주기와 자릿수로 샘플을 만든다 (설계 §4.4)."""
import time
from datetime import datetime, timedelta

from common.config_models import load_plant, load_runtime
from common.sim_clock import KST
from simulator.generator import SampleGenerator, make_signal_model
from simulator.rfcc_model import RfccSignalModel
from simulator.signal_model import SignalModel

T0 = datetime(2026, 11, 9, 10, 0, tzinfo=KST)


def test_model_factory_follows_plant_signal_model(plant_path, rfcc_plant_path):
    assert type(make_signal_model(load_plant(plant_path), 1, 10)) is SignalModel
    assert isinstance(make_signal_model(load_plant(rfcc_plant_path), 1, 1), RfccSignalModel)


def test_one_second_window_has_only_one_second_tags_except_on_ten_second_marks(rfcc_plant_path, rfcc_runtime_path):
    gen = SampleGenerator(load_plant(rfcc_plant_path), load_runtime(rfcc_runtime_path))
    at_mark = gen.generate(T0, T0 + timedelta(seconds=1))            # T0 는 10초 격자
    off_mark = gen.generate(T0 + timedelta(seconds=3), T0 + timedelta(seconds=4))
    assert len(at_mark) == 81 and len(off_mark) == 65
    assert all(s.ts.second % 10 == 0 for s in at_mark if s.tag_id.startswith("TI-"))


def test_hour_window_counts_and_speed(rfcc_plant_path, rfcc_runtime_path):
    gen = SampleGenerator(load_plant(rfcc_plant_path), load_runtime(rfcc_runtime_path))
    t = time.monotonic()
    samples = gen.generate(T0, T0 + timedelta(hours=1))
    assert time.monotonic() - t < 10.0
    assert len(samples) == 65 * 3600 + 16 * 360


def test_decimals_follow_tag_config(rfcc_plant_path, rfcc_runtime_path):
    gen = SampleGenerator(load_plant(rfcc_plant_path), load_runtime(rfcc_runtime_path))
    by_tag = {s.tag_id: s for s in gen.generate(T0, T0 + timedelta(seconds=1))}
    assert by_tag["ZI-E-1202"].value == round(by_tag["ZI-E-1202"].value, 5)
    assert len(str(by_tag["ZI-E-1202"].value).split(".")[1]) >= 4        # 3자리로 뭉개지지 않았다
    assert by_tag["VI-P-1101A"].value == round(by_tag["VI-P-1101A"].value, 4)
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_rfcc_generator.py -q`
Expected: FAIL — `make_signal_model` 없음.

- [ ] **Step 3: 구현**

`generate()` 의 열 수집에서 태그별 epoch 부분집합(마스크)을 쓰고, 행 조립 루프는 epoch 별로 `present` 와 마스크를 함께 본다. `_tag_series` 는 태그의 epoch 부분집합만 모델·시나리오에 넘긴다. `model_for_tests` 는 `make_signal_model` 을 쓴다.

- [ ] **Step 4: 통과 확인과 회귀**

Run: `uv run pytest -q`
Expected: 전부 통과 (hds 는 모든 태그 interval 10 = sample 10 이라 결과 그대로).

- [ ] **Step 5: Commit**

```bash
git add simulator/generator.py simulator/scenario.py tests/test_rfcc_generator.py
git commit -m "feat: 생성기 — 구성안의 signal_model 로 모델 선택, 태그별 주기·자릿수"
```

---

### Task 5: 시나리오 — `swell` 패턴, `repeat_every`, RFCC 시나리오 파일

**Files:**
- Modify: `simulator/patterns.py`, `common/scenario_models.py`, `simulator/scenario.py` (`_event_offset`, `apply` 의 `_mask`), `simulator/random_anomalies.py` (한계값 없는 태그 건너뜀)
- Create: `config/scenario_rfcc.yaml` (설계 §4.3 의 yaml 그대로)
- Modify: `tests/test_patterns.py:11-13` (패턴 8개)
- Test: `tests/test_scenario.py` 에 추가

**Interfaces:**
- Produces:
  - `patterns.swell(progress, epochs, magnitude)`; `VALUE_PATTERNS` 에 `swell` 추가; `PATTERNS` 8개.
  - `ScenarioEvent.repeat_every: timedelta | None = None` (문자열 기간 파싱 재사용). 검증: `recovered_at` 과 함께 쓰면 `ValueError("event X: repeat_every cannot be combined with recovered_at")`; `duration > repeat_every` 면 `ValueError`.
  - `ScenarioEvent.occurrence_start(epochs: np.ndarray) -> np.ndarray` — 각 epoch 가 속한(또는 직전) 회차의 시작 epoch. `repeat_every` 없으면 `start` 상수.
  - `_event_offset` 과 quality/freeze/gap 마스크가 회차 시작 기준으로 진행률·활성 구간을 계산한다.
  - `RandomAnomalyMaker._events`: `tag.limits is None` 이면 빈 목록.

- [ ] **Step 1: 실패하는 테스트**

`tests/test_patterns.py` 의 `test_all_seven_patterns_registered` 를 `test_all_eight_patterns_registered` 로 바꾸고 `swell` 을 두 집합에 넣는다. 추가:

```python
def test_swell_rises_holds_recovers():
    z = np.array([0.0, 0.125, 0.25, 0.5, 0.7, 0.85, 1.0 - 1e-9])
    out = shape("swell", z, np.zeros_like(z), 2.0)
    np.testing.assert_allclose(out, [0.0, 1.0, 2.0, 2.0, 2.0, 1.0, 0.0], atol=1e-6)
```

`tests/test_scenario.py` 에 추가:

```python
def test_repeat_every_repeats_the_event_weekly(rfcc_plant_path, rfcc_runtime_path, rfcc_scenario_path):
    plant, runtime = load_plant(rfcc_plant_path), load_runtime(rfcc_runtime_path)
    sc = load_scenarios([str(rfcc_scenario_path)], plant, runtime)
    first = datetime(2026, 11, 2, 5, 10, tzinfo=KST)
    e1 = epochs_range(first, first + timedelta(minutes=40), 1)
    e2 = e1 + 7 * 86400
    np.testing.assert_allclose(sc.deviation("VI-P-1101A", e1), sc.deviation("VI-P-1101A", e2))
    assert sc.deviation("VI-P-1101A", e1).max() > 1.8                                   # magnitude 1.85
    assert sc.deviation("PS-P-1101A", e1).min() < -0.8                                  # ratio −0.4595
    assert sc.deviation("VI-P-1101A", e1 + 3 * 86400).max() == 0.0                      # 회차 사이는 0


def test_repeat_every_rejects_recovered_at():
    with pytest.raises(ValueError, match="repeat_every cannot be combined"):
        ScenarioEvent(event_id="E", tag_id="X", pattern="swell", start=datetime(2026, 1, 1, tzinfo=KST), duration="1h",
                      repeat_every="7d", recovered_at=datetime(2026, 1, 2, tzinfo=KST))


def test_quality_event_repeats(rfcc_plant_path, rfcc_runtime_path, rfcc_scenario_path):
    plant, runtime = load_plant(rfcc_plant_path), load_runtime(rfcc_runtime_path)
    sc = load_scenarios([str(rfcc_scenario_path)], plant, runtime)
    equip = plant.equipment_by_id()["P-1302A"]
    start = datetime(2026, 11, 2, 23, 13, 20, tzinfo=KST) + timedelta(days=14)
    epochs = epochs_range(start - timedelta(seconds=5), start + timedelta(seconds=65), 1)
    _, qualities, _ = sc.apply(sc.model_for_tests(), equip, "VI-P-1302A", epochs)
    assert qualities[:5] == ["Good"] * 5 and qualities[5:65] == ["Bad"] * 60 and qualities[65:] == ["Good"] * 5


def test_random_anomalies_skip_tags_without_limits(rfcc_plant_path, rfcc_runtime_path):
    plant, runtime = load_plant(rfcc_plant_path), load_runtime(rfcc_runtime_path)
    maker = RandomAnomalyMaker(plant, runtime.seed, rate_per_tag_day=5.0)
    epochs = epochs_range(datetime(2026, 11, 9, tzinfo=KST), datetime(2026, 11, 10, tzinfo=KST), 60)
    assert maker.offset("VI-P-1101A", epochs).max() == 0.0
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_patterns.py tests/test_scenario.py -q`
Expected: FAIL — `swell` 없음, `repeat_every` extra forbidden, `rfcc_scenario_path` 파일 없음.

- [ ] **Step 3: 구현 + `config/scenario_rfcc.yaml` 작성**

`occurrence_start`: `k = floor((epoch − start)/repeat)`, `k < 0` 이면 0 으로 고정, `start + k·repeat`. `_event_offset` 은 `active = (epoch ≥ occ) & (epoch < occ + duration)`(repeat 가 있으면 `recovered_at` 이 없으므로 `end = occ + duration`). `apply` 의 sample 이벤트도 같은 마스크 함수를 쓴다. `seed_history` 는 손대지 않는다(`e.start`/`e.end` 가 첫 회차).

- [ ] **Step 4: 통과 확인과 회귀**

Run: `uv run pytest -q`
Expected: 전부 통과.

- [ ] **Step 5: Commit**

```bash
git add simulator/patterns.py common/scenario_models.py simulator/scenario.py simulator/random_anomalies.py config/scenario_rfcc.yaml tests/test_patterns.py tests/test_scenario.py
git commit -m "feat: 시나리오 — swell 패턴, repeat_every 반복, RFCC 이상 징후·품질 이벤트"
```

---

### Task 6: 수집 서버 — 10분 보관 삭제, 한계값 없는 태그, NoData 기준

**Files:**
- Modify: `collector/jobs.py` (`advance`, `_aggregate_10m`, `_retention`, `_evaluate_alarms`), `collector/app.py` (NoData 기준)
- Modify: `tests/test_jobs.py:89-98`
- Test: `tests/test_jobs.py`, `tests/test_collector_rfcc.py`

**Interfaces:**
- Consumes: `PlantConfig.tag_interval_sec`, `TagConfig.limits | None`.
- Produces:
  - `JobRunner.advance` 는 10m·1h 두 배치만 돈다. `_aggregate_10m(start, end)` 끝에 `self._retention(end)` 호출.
  - `JobRunner._retention(now: datetime) -> None`: `DELETE FROM ts_raw WHERE ts < now − raw_retention_days`; `DELETE FROM ingest_batch WHERE received_at < 벽시계 UTC now − raw_retention_days`.
  - `_evaluate_alarms` 는 `tag.limits is None` 인 태그를 건너뛴다. `max_delay` 는 한계값 있는 태그만으로 계산(없으면 1).
  - `collector/app.py`: `_status(row: dict | None, tag_id: str) -> str` — `row` 없으면 `NoData`; 나이 `> NO_DATA_SEND_INTERVALS × max(send_interval_sec, tag_interval_sec)` 면 `NoData`; 아니면 `row["quality"]`. `/tags/{id}/current` 가 이 함수를 쓴다.

- [ ] **Step 1: 실패하는 테스트**

`tests/test_jobs.py` 의 `test_daily_retention_deletes_old_raw` 를 아래로 바꾼다:

```python
def test_retention_runs_with_ten_minute_batch(env):
    storage, plant, jobs = env
    old = T0 - timedelta(days=8)
    _feed(storage, "VI-C1101", "C-1101", old, 10, 2.0)
    _feed(storage, "VI-C1101", "C-1101", T0, 10, 2.0)
    _feed(storage, "VI-C1101", "C-1101", T0 + timedelta(minutes=10), 1, 2.0)   # 10분 경계만 넘긴다
    jobs.advance(storage.sim_now())
    assert storage.query("SELECT MIN(ts) FROM ts_raw")[0][0] == to_db(T0)
    assert storage.query("SELECT COUNT(*) FROM ts_10m WHERE bucket_start < ?", (to_db(T0),))[0][0] > 0   # 집계는 남는다


def test_stale_daily_job_state_is_harmless(env):
    storage, plant, jobs = env
    storage.execute("INSERT INTO job_state VALUES ('daily', ?)", (to_db(T0),))
    _feed(storage, "VI-C1101", "C-1101", T0, 10, 2.0)
    _feed(storage, "VI-C1101", "C-1101", T0 + timedelta(minutes=10), 1, 2.0)
    assert jobs.advance(storage.sim_now()) >= 1
```

`tests/test_collector_rfcc.py` (새 파일):

```python
"""RFCC 구성안으로 뜬 수집 서버: 한계값 없는 태그, 10초 태그의 NoData 기준, 수신 묶음 정리."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from collector.app import create_app
from common.message import IngestBatch, Sample
from common.timefmt import to_db

T0 = datetime(2026, 11, 9, 1, 0, tzinfo=timezone.utc)


@pytest.fixture
def client(rfcc_plant_path, rfcc_runtime_path, tmp_path):
    app = create_app(rfcc_plant_path, rfcc_runtime_path, tmp_path / "rfcc.db")
    with TestClient(app) as c:
        yield c


def _send(client, tag_id, equip_id, start, count, step_sec, value):
    samples = [Sample(equip_id=equip_id, tag_id=tag_id, ts=start + timedelta(seconds=i * step_sec), value=value, quality="Good")
               for i in range(count)]
    batch = IngestBatch(source_id="T", batch_id=f"b-{tag_id}-{start.timestamp()}", sent_at=start, samples=samples)
    assert client.post("/ingest", content=batch.model_dump_json(), headers={"content-type": "application/json"}).status_code == 200


def test_ten_second_tag_is_good_until_three_of_its_intervals(client):
    _send(client, "TI-P-1101A", "P-1101A", T0, 1, 10, 66.0)
    _send(client, "VI-P-1101A", "P-1101A", T0, 10, 1, 1.45)                 # sim_now = T0+9s
    assert client.get("/tags/TI-P-1101A/current").json()["status"] == "Good"
    _send(client, "VI-P-1101A", "P-1101A", T0 + timedelta(seconds=31), 1, 1, 1.45)
    assert client.get("/tags/TI-P-1101A/current").json()["status"] == "NoData"


def test_no_alarms_without_limits_and_aggregates_exist(client):
    _send(client, "VI-P-1101A", "P-1101A", T0, 600, 1, 9.9)                 # 아무리 커도 한계값이 없다
    _send(client, "VI-P-1101A", "P-1101A", T0 + timedelta(minutes=10), 1, 1, 9.9)
    assert client.get("/alarms").json() == []
    st = client.app.state.storage
    assert st.query("SELECT COUNT(*) FROM ts_10m WHERE tag_id = 'VI-P-1101A' AND count > 0")[0][0] == 1


def test_old_ingest_batches_are_deleted(client):
    st = client.app.state.storage
    st.execute("INSERT INTO ingest_batch VALUES ('b-old', 'T', ?, 0)", (to_db(datetime.now(timezone.utc) - timedelta(days=2)),))
    _send(client, "VI-P-1101A", "P-1101A", T0, 1, 1, 1.45)
    _send(client, "VI-P-1101A", "P-1101A", T0 + timedelta(minutes=10), 1, 1, 1.45)
    assert st.query("SELECT COUNT(*) FROM ingest_batch WHERE batch_id = 'b-old'")[0][0] == 0
    assert st.query("SELECT COUNT(*) FROM ingest_batch")[0][0] == 2
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_jobs.py tests/test_collector_rfcc.py -q`
Expected: 보관 테스트·NoData 테스트·ingest_batch 테스트 FAIL. 알람 테스트는 `tag.limits.warn_hi` 에서 AttributeError.

- [ ] **Step 3: 구현**

`advance` 의 `daily` 호출 제거. `_retention` 시그니처를 `(now)` 로. `_evaluate_alarms` 의 태그 루프에 `if tag.limits is None: continue`. `app.py` 의 `tag_current` 를 `_status` 로 정리.

- [ ] **Step 4: 통과 확인과 회귀**

Run: `uv run pytest -q`
Expected: 전부 통과. `test_current_becomes_nodata_after_three_send_intervals`(hds) 도 그대로 통과(60 > 10).

- [ ] **Step 5: Commit**

```bash
git add collector/jobs.py collector/app.py tests/test_jobs.py tests/test_collector_rfcc.py
git commit -m "feat: 수집 서버 — 보관 삭제를 10분 배치로, 한계값 없는 태그는 알람 제외, 태그 주기 반영한 NoData"
```

---

### Task 7: 조회 API `/equipment`, `/current` 와 정적 페이지 제공

**Files:**
- Modify: `collector/app.py`
- Create: `collector/static/index.html` (Task 8 에서 채움. 여기서는 `<!doctype html><title>RFCC</title>` 자리표시)
- Test: `tests/test_collector_rfcc.py` 에 추가

**Interfaces:**
- Produces:
  - `GET /equipment` → `list[dict]`: `{equip_id, name, type, unit_id, unit_name, unit_group_id, unit_group_name, driven_by, tags: [{tag_id, signal, unit, description, interval_sec, decimals}]}`. 순서: `plant.unit_groups` → `units` → 그 Unit 의 설비(yaml 순서). `interval_sec` 는 `plant.tag_interval_sec(tag_id, runtime.sample_interval_sec)`.
  - `GET /current` → `list[dict]`: 모든 태그에 대해 `{tag_id, equip_id, ts, value, quality, received_at, status}`. 없는 태그는 `ts/value/quality/received_at = None`, `status = "NoData"`. 순서는 `/equipment` 와 같다.
  - `GET /` → `FileResponse(Path(__file__).with_name("static") / "index.html", media_type="text/html")`.

- [ ] **Step 1: 실패하는 테스트** (`tests/test_collector_rfcc.py` 에 추가)

```python
def test_equipment_listing_follows_hierarchy(client):
    equipment = client.get("/equipment").json()
    assert [e["equip_id"] for e in equipment][:3] == ["P-1101A", "B-1201", "E-1202"]
    assert equipment[0]["unit_group_name"] == "원료 공급" and equipment[1]["driven_by"] == "E-1202"
    tags = {t["tag_id"]: t for e in equipment for t in e["tags"]}
    assert len(tags) == 81 and tags["TI-P-1101A"]["interval_sec"] == 10 and tags["ZI-E-1202"]["decimals"] == 5
    assert tags["TI-P-1101A"]["description"] == "펌프 구동측 베어링 온도"


def test_current_lists_every_tag_with_status(client):
    _send(client, "VI-P-1101A", "P-1101A", T0, 1, 1, 1.45)
    rows = {r["tag_id"]: r for r in client.get("/current").json()}
    assert len(rows) == 81
    assert rows["VI-P-1101A"]["status"] == "Good" and rows["VI-P-1101A"]["value"] == 1.45
    assert rows["TI-P-1101A"]["status"] == "NoData" and rows["TI-P-1101A"]["value"] is None


def test_root_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html") and "<title>" in r.text
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_collector_rfcc.py -q`
Expected: 3 FAIL (404).

- [ ] **Step 3: 구현**

`create_app` 안에서 `equipment_listing = _equipment_listing(plant, runtime)` 를 한 번 만들어 `/equipment` 가 돌려주고, `/current` 는 `storage.query_dicts("SELECT * FROM tag_current")` 를 태그 순서로 맞춘다. `Dockerfile` 은 `collector` 폴더 전체를 복사하므로 정적 파일도 들어간다.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest -q`
Expected: 전부 통과.

- [ ] **Step 5: Commit**

```bash
git add collector/app.py collector/static/index.html tests/test_collector_rfcc.py
git commit -m "feat: 조회 API /equipment, /current 와 / 정적 페이지"
```

---

### Task 8: 디버깅 화면

**Files:**
- Modify: `collector/static/index.html`

**Interfaces:**
- Consumes: `/equipment`, `/current`, `/stream`(SSE `event: samples`, data = 샘플 배열 `{equip_id, tag_id, ts, value, quality}`).

- [ ] **Step 1: 페이지 작성**

의존성 없는 HTML+CSS+JS 한 파일. 설계 §4.6:
- 머리: 제목 `plant-simulator · 현재값`, `sim_now`(받은 샘플 ts 의 최대값, KST `YYYY-MM-DD HH:mm:ss`), SSE 상태(`연결됨` / `재연결 중`), 받은 샘플 수.
- `/equipment` 로 설비마다 `<table>`: 제목 `equip_id · name · unit_group_name / unit_name`, 열 `태그 ID | 설명 | 값 | 단위 | 품질 | 측정 시각(KST) | 주기`. 행 `id="tag-<tag_id>"`.
- `/current` 로 초기값. `EventSource("/stream")` 의 `samples` 이벤트로 행 갱신: 값은 `decimals` 자리 `toFixed`, Bad 는 값 칸 `—` 와 행 `class="bad"`, 갱신된 칸에 `class="flash"` 를 500 ms.
- 시각 표시: `new Date(ts).toLocaleString("sv-SE", {timeZone: "Asia/Seoul"})` (ISO 비슷한 형식).
- 스타일 최소: 고정폭 숫자, 표 간격, flash 배경, bad 흐림. 가로 스크롤 없이 1280px 에서 읽힘.

- [ ] **Step 2: 수동 확인**

터미널 1: `uv run python -m collector.app --plant config/plant_rfcc.yaml --runtime config/runtime_rfcc.yaml --scenario config/scenario_rfcc.yaml --db data/rfcc.db`
터미널 2: `uv run python -m simulator.main --plant config/plant_rfcc.yaml --runtime config/runtime_rfcc.yaml --scenario config/scenario_rfcc.yaml`
브라우저 `http://localhost:8000/`: 표 12개·81행, 1초 태그 값이 매초 바뀌고 10초 태그는 10초마다, 상단 시각이 매초 전진. 확인 뒤 두 프로세스 종료, `data/rfcc.db*` 삭제.
Expected: 위 동작. (자동 테스트는 Task 7 의 `/` 응답으로 충분.)

- [ ] **Step 3: Commit**

```bash
git add collector/static/index.html
git commit -m "feat: 디버깅 화면 — 81개 태그 현재값을 SSE 로 1초마다 갱신"
```

---

### Task 9: 실행 구성, 문서, 실제 실행 확인

**Files:**
- Modify: `docker-compose.yml`, `.gitignore`, `README.md`, `pyproject.toml`(description)
- Modify: `collector/app.py`·`simulator/main.py` 기본 인자는 그대로(`plant_hds`). compose 만 RFCC 기본.

- [ ] **Step 1: `.gitignore` 에 `rfcc-20261109/` 추가, `docker-compose.yml` 환경 변수화**

```yaml
# 기본은 RFCC 구성안. plant_hds 로 돌리려면 PLANT_CONFIG=config/plant_hds.yaml RUNTIME_CONFIG=config/runtime.yaml
# SCENARIO_CONFIG=config/scenario_dev.yaml DB_PATH=data/plant.db 를 준다.
services:
  collector:
    command: python -m collector.app --plant ${PLANT_CONFIG:-config/plant_rfcc.yaml} --runtime ${RUNTIME_CONFIG:-config/runtime_rfcc.yaml}
             --scenario ${SCENARIO_CONFIG:-config/scenario_rfcc.yaml} --db ${DB_PATH:-data/rfcc.db} --port 8000
  simulator:
    command: python -m simulator.main --plant ${PLANT_CONFIG:-config/plant_rfcc.yaml} --runtime ${RUNTIME_CONFIG:-config/runtime_rfcc.yaml}
             --scenario ${SCENARIO_CONFIG:-config/scenario_rfcc.yaml} --time-mode ${SIM_TIME_MODE:-backfill}
```

나머지(볼륨, healthcheck, restart, depends_on, COLLECTOR_URL)는 그대로.

- [ ] **Step 2: README 갱신**

머리 설명을 "구성안 두 개(RFCC 기본, 수첨탈황)"로. 추가 절: RFCC 구성안(설비 12·태그 81, 1초/10초, 출처 폴더, 한계값 없음), 화면 `/`, API 표에 `/equipment`·`/current`·`/`, DB 절의 보관 규칙(10분마다, 하루, `ingest_batch` 도), 설정 파일 표에 `plant_rfcc.yaml`·`runtime_rfcc.yaml`·`scenario_rfcc.yaml`, 구성안 필드 추가분(`signal_model`, `interval_sec`, `decimals`, `description`, 설비 `seed_key`, 유형 `kind`, 선택적 `limits`), 시나리오 `swell`·`repeat_every`, 신호 생성 절에 RFCC 모델 한 문단, 설계 문서 링크.

- [ ] **Step 3: 로컬 실제 실행 (백필 1시간 + 실시간 30초)**

`runtime_rfcc.yaml` 복사본(스크래치)에서 `backfill_days` 대신 짧은 백필을 쓸 수 없으므로(일 단위), 수집 서버를 띄운 뒤 시뮬레이터를 `--time-mode backfill` 로 돌리고 로그에서 첫 1시간 묶음 전송 시간을 읽는다:

```bash
uv run python -m collector.app --plant config/plant_rfcc.yaml --runtime config/runtime_rfcc.yaml --scenario config/scenario_rfcc.yaml --db data/rfcc.db &
sleep 2
uv run python -m simulator.main --plant config/plant_rfcc.yaml --runtime config/runtime_rfcc.yaml --scenario config/scenario_rfcc.yaml --time-mode backfill &
```

Expected: 로그 `sent b-... samples=239760 stored=239760 rejected=0` 이 묶음당 30초 안에 찍히고(타임아웃 재시도 로그 없음), 24묶음 뒤 `switch to live`. 이후 `curl -s localhost:8000/current | python3 -c "import json,sys; r=json.load(sys.stdin); print(len(r), sum(x['status']=='Good' for x in r))"` → `81 81`. `curl -s localhost:8000/health` 의 `sim_now` 가 현재 시각 근처, `catching_up: false`. `ls -la data/rfcc.db` 로 하루치 크기를 기록해 README 의 보관 절에 적는다(예상 400~600 MB). 두 프로세스 종료.

- [ ] **Step 4: Docker Compose 확인**

`docker compose up -d --build` → `/health` ok, `/` 에서 값 갱신 확인(백필 중에는 `catching_up` 일 수 있음). `docker compose down`.

- [ ] **Step 5: 전체 테스트와 Commit**

Run: `uv run pytest -q`
Expected: 전부 통과.

```bash
git add docker-compose.yml .gitignore README.md pyproject.toml
git commit -m "docs: RFCC 구성안 실행 방법 — compose 기본값, 화면, API, 보관 규칙"
```
