# Nelson 8종 이상 생성·판정 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 정상 신호를 Nelson 기준(고정 μ·σ, 위반 억제 표집)으로 바꾸고, 자동 이상(설비당 ~10%)·지속 고장·주입 이상을 만들며, 수집 서버가 원시 표본에 Nelson 8종을 상시 판정해 `/alarms` 로 낸다.

**Architecture:** 판정 규칙은 `common/nelson.py` 한 곳(표본 단위 `NelsonState` + 배열 단위 `judge`). 시뮬레이터는 태그별 z 를 순차 생성하고 사건(`simulator/anomaly.py`)을 얹어 `status` 를 채운다. 수집 서버는 받을 때마다 `judge` 로 `nelson_alarm` 을 열고 닫으며, 통합 실행에서는 `AnomalyBook` 이 주입·멈춤·지속 고장·정답지를 맡아 생성기에 사건을 넘긴다.

**Tech Stack:** Python 3.11, NumPy, Pydantic 2, FastAPI, SQLite, pytest(+asyncio). 새 런타임 의존성 없음.

**Spec:** `docs/superpowers/specs/2026-11-12-nelson-anomaly-design.md`

## Global Constraints

- 새 런타임 의존성 금지. 테스트는 `uv run pytest -q`.
- 모듈 머리는 한 문단 한국어 docstring, 주석은 "무엇인지"만. 기존 코드 스타일(간결, 파일 분리)을 따른다.
- 규칙 비트: 규칙 r 은 `1 << (r - 1)`. 판정은 보낸 값(구성안 `decimals` 로 반올림)에서 `z = (value − mean) / sigma` 로 한다.
- 경계와 같은 값은 '밖'이 아니다. 같은 값은 추세·교대를 끊는다. 조건이 성립한 마지막 표본에 표시한다.
- 정상 AR(1): `z = ρ·clip(z_prev, −2, 2) + √(1−ρ²)·N(0,1)`, ρ 0.12(온도 신호 0.25), 최대 100번 다시 뽑고 안 되면 `N(0,1)` 한 번 뽑은 값을 그대로.
- 자동 이상: 설비·KST 날짜마다 12건, 사건 시간 합 = 86400 × 0.105 = 9072초, 길이 lognormal(ln 650, 0.52)를 200~1800초로 자른 뒤 비례 조정, 간격 Dirichlet(3)×13.
- 지속 고장 간격 기본 15분(`persistent_fault_interval_min`), 환경 변수 `PERSISTENT_FAULT_INTERVAL_MIN` 이 덮어쓰고 0 이면 끈다. 백필 중에는 열지 않는다.
- 주입 시작 = 시뮬레이터 현재 시각(`sim_now`)을 초 단위로 올림 + 3초.
- 알람 묶음: (태그, 규칙)마다 15 표본 연속 그 규칙이 없으면 닫고 `cleared_at` = 그 15 표본 중 첫 표본 시각.
- 메시지 `schema_version` "2.0", `status: Literal["Good", "Bad"]`, `value` 필수. DB 스키마 v3.
- 심각도·정비·고장 이력 없음. `/alarms` 에 원인·분류·조치·사건 ID 를 싣지 않는다.
- `decimals` 는 `10**-decimals ≤ 0.1·sigma` 가 되도록 올린다(내리지 않는다).

## Review Focus

1. 반올림이 거친 반응 태그(윤활유 온도 등)에서도 템플릿이 그 태그의 반응 규칙을 실제로 성립시킨다 → Task 3 전 공장 전수 테스트.
2. 재시작: 열린 주입·지속 고장이 이어지고, 정상 신호 이음매에서 규칙 위반이 생기지 않는다 → Task 5 `test_restore_seam_has_no_violation`, Task 9 `test_book_reload_keeps_open_events`.
3. 자동 이상이 진행 중인 설비에 주입하면 자동 이상은 주입 시작에서 끝나고 정답지도 그렇게 적힌다 → Task 3 `test_resolve_truncates_spontaneous`, Task 9 `test_inject_truncates_active_spontaneous`.
4. 멈춤을 두 번 부르거나 사건이 없을 때: 설비 멈춤은 404, 전체 멈춤은 200 + 빈 목록 → Task 9 `test_stop_twice_and_empty`.
5. 5분 조회 창: `from` 이전에 열려 아직 열린 알람은 나오고, `from` 이전에 닫힌 알람은 안 나온다 → Task 8 `test_alarms_overlap_window`.

---

### Task 1: Nelson 판정기

**Files:**
- Create: `common/nelson.py`
- Test: `tests/test_nelson.py`

**Interfaces:**
- Produces:
  - `HISTORY = 14` (판정에 필요한 이전 표본 수)
  - `bit(rule: int) -> int`
  - `zscore(value: float, mean: float, sigma: float) -> float`
  - `class NelsonState: __init__(self, history: Iterable[float] = ())`, `peek(self, z: float) -> int`, `push(self, z: float) -> int`, `tail(self) -> list[float]` (최근 최대 14개 z)
  - `judge(z: np.ndarray, tail: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]` — `(hits, low)` 둘 다 `uint8`, 길이 `len(z)`. `low` 는 규칙 1·2·3·5·6 이 아래쪽(감소)으로 성립한 비트.
  - `QUIET: tuple[float, ...] = (0.3, 0.6, -0.4, -1.2, 0.2, -0.7, 1.1, -0.3)` — 반복해도 어떤 규칙도 성립하지 않는 z 패턴(테스트용 공용 상수)

- [ ] **Step 1: 실패하는 테스트 작성**

```python
import numpy as np
import pytest
from common.nelson import QUIET, NelsonState, bit, judge

def bits_of(seq):
    s = NelsonState()
    return [s.push(z) for z in seq]

def test_quiet_pattern_never_fires():
    assert set(bits_of(list(QUIET) * 30)) == {0}

@pytest.mark.parametrize("seq, rule, at", [
    ([3.0001], 1, 0),
    ([0.5] * 9, 2, 8),
    ([0, 0.1, 0.2, 0.3, 0.4, 0.5], 3, 5),
    ([0.5, -0.5] * 7, 4, 13),
    ([2.1, 0.0, 2.1], 5, 2),
    ([1.1, 1.1, 0.0, 1.1, 1.1], 6, 4),
    ([0.5, 0.5, -0.5, -0.5] * 4, 7, 14),
    ([1.5, -1.5, 1.5, 1.5, -1.5, -1.5, 1.5, -1.5], 8, 7),
])
def test_each_rule_fires_on_last_sample(seq, rule, at):
    b = bits_of(seq)
    assert b[at] & bit(rule)
    assert not any(x & bit(rule) for x in b[:at])

@pytest.mark.parametrize("seq, rule", [
    ([3.0], 1),                                  # 경계는 밖이 아니다
    ([0.5] * 4 + [0.0] + [0.5] * 4, 2),          # 0 은 어느 쪽도 아니다
    ([0, 0.1, 0.1, 0.2, 0.3, 0.4], 3),           # 같은 값은 추세를 끊는다
    ([2.0, 0.0, 2.1], 5),
])
def test_boundaries_do_not_fire(seq, rule):
    assert not any(x & bit(rule) for x in bits_of(seq))

def test_peek_does_not_change_state():
    s = NelsonState([0.5] * 8)
    assert s.peek(0.5) & bit(2)
    assert s.peek(-0.5) & bit(2) == 0
    assert s.push(0.5) & bit(2)

def test_judge_matches_state_and_tail():
    rng = np.random.default_rng(1)
    z = np.concatenate([rng.normal(size=3000), np.full(20, 0.6), np.tile([0.7, -0.7], 20),
                        np.linspace(-2.4, 2.4, 20), rng.normal(0, 3, 500)])
    hits, _ = judge(z)
    assert list(hits) == bits_of(z)
    tail_hits, _ = judge(z[100:], tail=z[86:100])
    assert np.array_equal(tail_hits, hits[100:])

def test_judge_low_bits():
    hits, low = judge(np.array([-3.5]))
    assert hits[0] & bit(1) and low[0] & bit(1)
```

- [ ] **Step 2: 실행해서 실패 확인**

Run: `uv run pytest tests/test_nelson.py -q`
Expected: FAIL (`ModuleNotFoundError: common.nelson`)

- [ ] **Step 3: `common/nelson.py` 구현**

`NelsonState` 는 최근 15개 z 를 deque 로 들고 규칙마다 최대 15개만 다시 센다(표본당 상수 시간). `judge` 는 `concat(tail, z)` 에 data-v3 `build_dataset.nelson()` 과 같은 누적합 창 계산을 하고 앞의 `len(tail)` 개를 잘라 낸다. 다만 품질 제외 블록은 빼고, 부호별 루프에서 `low` 비트를 함께 채운다.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_nelson.py -q`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add common/nelson.py tests/test_nelson.py
git commit -m "feat: Nelson 8종 판정기 (표본 단위 상태 + 배열 판정)"
```

---

### Task 2: 구성안에 관리 기준(μ·σ) 넣기

**Files:**
- Modify: `common/config_models.py` (`ControlLimits` 추가, `TagConfig.control`, `RuntimeConfig.persistent_fault_interval_min`)
- Modify: `config/plant_{rfcc,cdu,hou,rhds,vrds}.yaml`, `config/runtime_{rfcc,cdu,hou,rhds,vrds}.yaml`
- Modify: `collector/plant_context.py` (`equipment_listing` 에 `control`)
- Test: `tests/test_control_config.py`

**Interfaces:**
- Produces:
  - `class ControlLimits(StrictModel): mean: float; sigma: float = Field(gt=0)`
  - `TagConfig.control: ControlLimits | None = None` (Task 6 에서 필수로 바꾼다. HDS 가 아직 남아 있으므로)
  - `RuntimeConfig.persistent_fault_interval_min: float = Field(default=15, ge=0)`
  - `/equipment` 태그 항목에 `"control": {"mean", "sigma"}`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
import json, math
from pathlib import Path
import pytest
from common.config_models import load_plant, load_runtime

PLANTS = ["rfcc", "cdu", "hou", "rhds", "vrds"]
BASELINES = Path.home() / "Downloads/data-v3/평가전용/nelson-20261112/baselines.json"

@pytest.mark.parametrize("key", PLANTS)
def test_every_tag_has_control_and_fine_decimals(config_dir, key):
    plant = load_plant(config_dir / f"plant_{key}.yaml")
    for t in plant.all_tags():
        assert t.control is not None, t.tag_id
        assert 10 ** -t.decimals <= 0.1 * t.control.sigma + 1e-12, t.tag_id

def test_rfcc_limits_are_three_sigma(config_dir):
    for t in load_plant(config_dir / "plant_rfcc.yaml").all_tags():
        c = t.control
        assert t.limits.warn_lo == pytest.approx(c.mean - 3 * c.sigma, abs=1e-4)
        assert t.limits.warn_hi == pytest.approx(c.mean + 3 * c.sigma, abs=1e-4)

@pytest.mark.parametrize("key", PLANTS)
def test_runtime_persistent_interval_default(config_dir, key):
    assert load_runtime(config_dir / f"runtime_{key}.yaml").persistent_fault_interval_min == 15

@pytest.mark.skipif(not BASELINES.exists(), reason="data-v3 자료 없음")
def test_control_matches_baselines(config_dir):
    base = {(b["plant"], b["equipment"], b["tag"]): b for b in json.loads(BASELINES.read_text())}
    for key in PLANTS:
        plant = load_plant(config_dir / f"plant_{key}.yaml")
        for e in plant.equipment:
            for t in e.tags:
                b = base[(key.upper(), e.equip_id, t.tag_id)]
                assert (t.control.mean, t.control.sigma) == (b["mean"], b["sigma"])
```

- [ ] **Step 2: 실행해서 실패 확인**

Run: `uv run pytest tests/test_control_config.py -q`
Expected: FAIL (`TagConfig` 에 `control` 없음)

- [ ] **Step 3: 모델 추가**

`ControlLimits`, `TagConfig.control`, `RuntimeConfig.persistent_fault_interval_min` 을 위 Interfaces 대로 넣는다. `equipment_listing` 의 태그 dict 에 `"control": t.control.model_dump() if t.control else None` 을 더한다.

- [ ] **Step 4: 구성안 변환 (일회성 스크립트, 커밋하지 않는다)**

scratchpad 에 스크립트를 두고 yaml 을 텍스트로 고쳐 주석·배치를 보존한다. 태그마다:
- `seed_key: …,` 다음 줄에 `control: { mean: <baselines mean>, sigma: <baselines sigma> },` 를 넣는다(`normal:` 줄은 Task 6 에서 지운다).
- `decimals` 를 `max(현재, ceil(-log10(0.1·sigma)))` 로 올린다.
- RFCC 는 `limits: { warn_lo: round(μ−3σ, 4), warn_hi: round(μ+3σ, 4) }`, `limit_basis: "Nelson 관리한계 μ±3σ (baselines.json)"`. 파일 머리 주석의 한계값 설명도 같은 뜻으로 고친다.
- 각 `plant_*.yaml` 의 `config_version` 을 1 올린다.

`runtime_*.yaml` 다섯 개에 `persistent_fault_interval_min: 15   # 지속 고장을 여는 간격(분). 0 이면 끈다` 를 넣는다.

- [ ] **Step 5: 통과 확인 (전체)**

Run: `uv run pytest -q`
Expected: 기존 160개 + 새 테스트 모두 PASS. `decimals` 가 바뀐 태그의 값을 고정해 둔 기존 테스트가 깨지면 기대값만 새 자릿수로 고친다.

- [ ] **Step 6: 커밋**

```bash
git add common/config_models.py collector/plant_context.py config/ tests/test_control_config.py
git commit -m "feat: 구성안에 Nelson 관리 기준(μ·σ)·자릿수·RFCC 3σ 한계와 지속 고장 간격"
```

---

### Task 3: 이상 사건 모양·반응 태그·분류

**Files:**
- Create: `simulator/anomaly.py`
- Test: `tests/test_anomaly.py`

**Interfaces:**
- Consumes: Task 1 `NelsonState`, `zscore`, `bit`. Task 2 `TagConfig.control`.
- Produces:
  - `Origin = Literal["spontaneous", "persistent", "injected"]`
  - `@dataclass(frozen=True) class AnomalyEvent: event_id: str; origin: Origin; equip_id: str; rule: int; start: float; end: float | None; normal_change: bool = False` (`start`·`end` 는 epoch 초, `end=None` 은 멈춤 전까지)
  - `@dataclass(frozen=True) class Response: rule: int; direction: float; lag_sec: int; recovery_sec: int`
  - `SAWTOOTH = 20`, `MAX_RECOVERY_SEC = 60`
  - `primary_tag(plant: PlantConfig, equip_id: str) -> str` — 설비의 첫 `vibration`/`shaft_vibration` 태그
  - `responses(plant: PlantConfig, equip_id: str, rule: int) -> dict[str, Response]` — tag_id → 반응
  - `template_z(rule: int, k: int, u: float) -> float`
  - `tag_z(event: AnomalyEvent, resp: Response, t: float, interval: int, u: float) -> float | None` — 반응 구간 밖이면 `None`
  - `classify(rule: int, normal_change: bool) -> tuple[str, str, str]` — (kind, cause, action)
  - `resolve(events: list[AnomalyEvent], stops: dict[str, float]) -> list[AnomalyEvent]` — 멈춤 반영 + 같은 설비의 주입·지속 고장과 겹치는 자동 이상을 자른 결과

반응 표(`build_dataset.py` `response()` 그대로):

| 대상 | 반응 규칙 | 방향 | 지연 | 회복 |
| --- | --- | --- | --- | --- |
| 대표 태그 | 사건 규칙 | +1 | 0 | 규칙 7 이면 0, 아니면 5 |
| 규칙 7 의 다른 태그 | 없음 | | | |
| `speed` | 없음 | | | |
| 규칙 1·2·3·5·6 의 `bearing_temp`·`oil_temp` | 3 이면 3, 아니면 2 | +1 | 40 | 60 |
| 규칙 1·2·3·5·6 의 `vibration`·`shaft_vibration`·`current`·`oil_pressure` | 사건 규칙 | `oil_pressure` −1, 나머지 +1 | 5 | 10 |
| 규칙 4·8 의 `suction_pressure`·`discharge_pressure`·`flow`·`current`·`differential_pressure`·`vibration`·`shaft_vibration` | 사건 규칙 | `flow`·`suction_pressure` −1, 나머지 +1 | 2 | 15 |

템플릿(`signal_engine.cpp`, 3 만 톱니): 1 `3.5+0.12u` / 2 `0.65+0.18u` / 3 `-2.4 + 4.8·(k % 20)/19 + u/20` / 4 `(+1 if k%2 else -1)·(0.7+0.12u)` / 5 `-0.25 if k%3==2 else 2.35+0.15u` / 6 `-0.3 if k%5==4 else 1.55+0.17u` / 7 `0.025` / 8 `(+1 if k%4<2 else -1)·(1.55+0.18u)`.
`tag_z`: `k = (t − start − lag) // interval`. `t < start+lag` 이면 None. `end` 가 있고 `t ≥ end` 이면 `f = (end + recovery − t)/recovery` 로 `template·direction·f + 0.08u`, `f ≤ 0` 이거나 recovery 0 이면 None.
분류: 7 → ("계측 이상", "대표 진동 센서 값 고착", "센서·신호 변환기 점검 및 복구"), 8 → ("계측·제어 이상", "신호원 혼입 또는 제어계통의 두 상태 왕복", "신호원·제어계통 점검 및 복구"), 4 → ("기계·제어 이상", "유량·압력 제어 헌팅", "제어 밸브·제어기 점검 및 복구"), 그 밖 → ("기계 이상", "베어링·윤활 계통의 간헐적 성능 저하", "윤활 공급·기계 상태 점검 및 복구"), `normal_change` 이면 ("정상 운전 변화", "의도한 운전 부하 변경", "부하 조건 복귀; 기계 수리 아님").

- [ ] **Step 1: 실패하는 테스트 작성**

```python
import numpy as np
import pytest
from common.config_models import load_plant
from common.nelson import NelsonState, bit, zscore
from simulator.anomaly import AnomalyEvent, Response, primary_tag, resolve, responses, tag_z

def ev(rule, start=0.0, end=None, origin="injected", equip="C-3101", eid="E1"):
    return AnomalyEvent(eid, origin, equip, rule, start, end)

def test_rhds_c3101_response_tags_match_data_v3(config_dir):
    plant = load_plant(config_dir / "plant_rhds.yaml")
    assert primary_tag(plant, "C-3101") == "XI-X-C3101"
    assert set(responses(plant, "C-3101", 1)) == {"XI-X-C3101", "TI-B-C3101", "PI-LO-C3101", "TI-LO-C3101"}
    assert set(responses(plant, "C-3101", 4)) == {"XI-X-C3101", "PI-S-C3101", "PI-D-C3101", "FI-C3101"}
    assert set(responses(plant, "C-3101", 7)) == {"XI-X-C3101"}
    assert responses(plant, "C-3101", 1)["TI-B-C3101"] == Response(2, 1.0, 40, 60)
    assert responses(plant, "C-3101", 1)["PI-LO-C3101"] == Response(1, -1.0, 5, 10)

@pytest.mark.parametrize("key", ["rfcc", "cdu", "hou", "rhds", "vrds"])
def test_every_response_fires_its_rule_after_rounding(config_dir, key):
    plant = load_plant(config_dir / f"plant_{key}.yaml")
    tags = plant.tag_by_key()
    rng = np.random.default_rng(0)
    for e in plant.equipment:
        for rule in range(1, 9):
            for tag_id, resp in responses(plant, e.equip_id, rule).items():
                t = tags[(e.equip_id, tag_id)]
                iv = t.interval_sec or 1
                state, fired = NelsonState(), 0
                for i in range(40):
                    z = tag_z(ev(rule, equip=e.equip_id), resp, resp.lag_sec + i * iv, iv, rng.uniform(-1, 1))
                    v = round(t.control.mean + t.control.sigma * z, t.decimals)
                    fired |= state.push(zscore(v, t.control.mean, t.control.sigma))
                assert fired & bit(resp.rule), (key, e.equip_id, tag_id, rule)

def test_lag_and_recovery():
    r = Response(2, 1.0, 40, 60)
    assert tag_z(ev(2, start=100, end=500), r, 139, 10, 0.0) is None
    assert tag_z(ev(2, start=100, end=500), r, 140, 10, 0.0) == pytest.approx(0.65)
    assert tag_z(ev(2, start=100, end=500), r, 530, 10, 0.0) == pytest.approx(0.65 * 0.5)
    assert tag_z(ev(2, start=100, end=500), r, 560, 10, 0.0) is None
    assert tag_z(ev(7, end=50), Response(7, 1.0, 0, 0), 50, 1, 0.0) is None

def test_resolve_truncates_spontaneous():
    sp = ev(2, start=0, end=1000, origin="spontaneous", eid="SP")
    inj = ev(1, start=300, origin="injected", eid="INJ")
    other = ev(3, start=0, end=1000, origin="spontaneous", equip="P-3101", eid="SP2")
    out = {e.event_id: e for e in resolve([sp, inj, other], {"SP2": 400.0})}
    assert out["SP"].end == 300 and out["INJ"].end is None and out["SP2"].end == 400
    assert "SP3" not in {e.event_id for e in resolve([ev(1, start=500, end=600, origin="spontaneous", eid="SP3"), inj], {})}
```

- [ ] **Step 2: 실행해서 실패 확인**

Run: `uv run pytest tests/test_anomaly.py -q`
Expected: FAIL (`ModuleNotFoundError: simulator.anomaly`)

- [ ] **Step 3: `simulator/anomaly.py` 구현** (위 표·템플릿·분류 그대로)

`resolve`: 먼저 `stops` 로 `end = min(end or inf, stop)`. 같은 설비의 주입·지속 고장(`origin != "spontaneous"`)이 자동 이상 시작 시점에 이미 살아 있으면 그 자동 이상을 빼고, 자동 이상 도중에 시작하면 그 시작에서 자른다.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_anomaly.py -q`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add simulator/anomaly.py tests/test_anomaly.py
git commit -m "feat: 이상 사건 모양·반응 태그·분류 (data-v3 방식)"
```

---

### Task 4: 자동 이상 일정

**Files:**
- Create: `simulator/schedule.py`
- Test: `tests/test_schedule.py`

**Interfaces:**
- Consumes: Task 3 `AnomalyEvent`
- Produces: `EVENTS_PER_DAY = 12`, `EVENT_FRACTION = 0.105`, `daily_events(seed: int, equip_id: str, day: date) -> list[AnomalyEvent]` (KST 날짜, `origin="spontaneous"`, `end` 있음, 시작 순)

- [ ] **Step 1: 실패하는 테스트 작성**

```python
from datetime import date, datetime
from common.sim_clock import KST
from simulator.schedule import daily_events

D = date(2026, 11, 12)

def test_daily_events_shape():
    evs = daily_events(7, "C-3101", D)
    day0 = datetime(2026, 11, 12, tzinfo=KST).timestamp()
    assert len(evs) == 12
    assert sum(e.end - e.start for e in evs) == 9072
    assert all(day0 <= e.start < e.end <= day0 + 86400 for e in evs)
    assert all(a.end <= b.start for a, b in zip(evs, evs[1:]))
    assert {e.rule for e in evs} == set(range(1, 9))
    assert all(e.event_id.startswith("SP-C-3101-20261112-") for e in evs)
    assert all(not e.normal_change or e.rule == 2 for e in evs)

def test_daily_events_deterministic_and_distinct():
    assert daily_events(7, "C-3101", D) == daily_events(7, "C-3101", D)
    assert daily_events(7, "C-3101", D) != daily_events(7, "P-3101", D)
```

- [ ] **Step 2: 실행해서 실패 확인**

Run: `uv run pytest tests/test_schedule.py -q`
Expected: FAIL

- [ ] **Step 3: `daily_events` 구현**

`rng = np.random.default_rng([seed, zlib.crc32(equip_id.encode()), day.toordinal()])`. 길이·간격은 Global Constraints 대로, 정수로 반올림한 뒤 오차는 마지막 항목에 몰아 합이 정확히 9072·(86400−9072)가 되게 한다. 규칙은 `(offset + k) % 8 + 1`(`offset = rng.integers(8)`), `normal_change = rule == 2 and rng.random() < 0.2`, id 는 `SP-{equip_id}-{day:%Y%m%d}-{k+1:02d}`.

- [ ] **Step 4: 통과 확인** — Run: `uv run pytest tests/test_schedule.py -q` → PASS

- [ ] **Step 5: 커밋**

```bash
git add simulator/schedule.py tests/test_schedule.py
git commit -m "feat: 설비·날짜별 자동 이상 일정 (하루 12건, 10.5%)"
```

---

### Task 5: Nelson 신호 생성기

**Files:**
- Create: `simulator/nelson_model.py`
- Test: `tests/test_nelson_model.py`

**Interfaces:**
- Consumes: Task 1 `NelsonState`, `zscore`, `HISTORY`. Task 3 `AnomalyEvent`, `responses`, `tag_z`, `resolve`. Task 4 `daily_events`.
- Produces:
  - `class EventSource(Protocol): external(self, start: float, end: float) -> list[AnomalyEvent]; stops(self) -> dict[str, float]; opened(self, event: AnomalyEvent) -> None`
  - `class NoEvents` (EventSource, 아무 사건 없음)
  - `class NelsonGenerator: __init__(self, plant: PlantConfig, runtime: RuntimeConfig, source: EventSource | None = None, spontaneous: bool = True)`, `generate(self, start: datetime, end: datetime) -> list[Sample]`, `restore(self, last_values: dict[TagKey, list[float]]) -> None`
  - 이 Task 에서 `Sample.quality` 는 `"Good"`/`"Bad"` (Task 7 에서 `status` 로 이름만 바꾼다)

생성 순서(태그마다, 측정 시각 순): ① 그 시각에 걸린 사건의 `tag_z` 가 있으면 그 z, 없으면 AR(1) 억제 표집. 억제 표집은 후보 값을 반올림한 뒤 `state.peek` 로 확인한다. ② 값 = `round(mean + sigma·z, decimals)`, `z' = zscore(값)`. ③ `bits = state.push(z')`, `quality = "Bad" if bits else "Good"`, AR 의 직전 값은 `z'`.
- 난수: 태그마다 `default_rng([runtime.seed, crc32(seed_key), KST 날짜 ordinal])`, 날짜가 바뀌면 다시 시드. `restore` 뒤에는 시드에 복원 시각 epoch 를 더한다.
- 사건: 창마다 `source.external(start, end)` + 그 창이 걸친 날짜의 `daily_events`(spontaneous 가 True 일 때) → `resolve(..., source.stops())`. 자동 이상의 첫 표본을 만들 때 `source.opened(event)` 를 한 번 부른다. 끝난 사건도 `MAX_RECOVERY_SEC` 동안은 회복 때문에 계속 본다.
- `driven_by` 설비의 `speed` 태그는 구동기 `speed` 태그의 z 를 그대로 쓴다.
- `runtime.units` 와 태그별 `interval_sec` 는 기존 `SampleGenerator` 와 같은 방식(측정 시각이 주기 격자에 걸릴 때만 표본)이다.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
from datetime import datetime, timedelta
import numpy as np
import pytest
from common.config_models import load_plant, load_runtime
from common.nelson import NelsonState, zscore
from common.sim_clock import KST
from simulator.anomaly import AnomalyEvent
from simulator.nelson_model import NelsonGenerator, NoEvents

T0 = datetime(2026, 11, 12, 0, 0, tzinfo=KST)

class Fixed(NoEvents):
    def __init__(self, events, stops=None):
        self.events, self._stops, self.seen = events, stops or {}, []
    def external(self, start, end): return self.events
    def stops(self): return self._stops
    def opened(self, e): self.seen.append(e.event_id)

@pytest.fixture
def rhds(config_dir):
    return load_plant(config_dir / "plant_rhds.yaml"), load_runtime(config_dir / "runtime_rhds.yaml")

def test_normal_is_quiet_and_on_grid(rhds):
    plant, rt = rhds
    s = NelsonGenerator(plant, rt, spontaneous=False).generate(T0, T0 + timedelta(hours=2))
    assert sum(x.quality == "Bad" for x in s) / len(s) < 0.005
    tags = plant.tag_by_key()
    assert all(round(x.value, tags[(x.equip_id, x.tag_id)].decimals) == x.value for x in s)

def test_injected_event_marks_only_its_equipment(rhds):
    plant, rt = rhds
    t = T0.timestamp() + 60
    g = NelsonGenerator(plant, rt, Fixed([AnomalyEvent("I1", "injected", "C-3101", 1, t, None)]), spontaneous=False)
    s = g.generate(T0, T0 + timedelta(minutes=5))
    bad = {(x.equip_id, x.tag_id) for x in s if x.quality == "Bad" and x.ts.timestamp() >= t}
    assert ("C-3101", "XI-X-C3101") in bad
    assert all(e == "C-3101" for e, _ in bad)
    assert next(x for x in s if x.tag_id == "XI-X-C3101" and x.ts.timestamp() == t).quality == "Bad"

def test_stop_returns_equipment_to_normal(rhds):
    plant, rt = rhds
    t = T0.timestamp() + 60
    g = NelsonGenerator(plant, rt, Fixed([AnomalyEvent("I1", "injected", "C-3101", 6, t, t + 120)]), spontaneous=False)
    s = g.generate(T0, T0 + timedelta(minutes=10))
    assert not [x for x in s if x.equip_id == "C-3101" and x.quality == "Bad" and x.ts.timestamp() >= t + 120 + 60 + 150]

def test_spontaneous_union_ratio_near_ten_percent(rhds):
    plant, rt = rhds
    rt = rt.model_copy(update={"units": [plant.equipment_by_id()["C-3101"].unit_id]})
    src = Fixed([])
    s = NelsonGenerator(plant, rt, src).generate(T0, T0 + timedelta(days=1))
    iv = {k: (t.interval_sec or 1) for k, t in plant.tag_by_key().items()}
    for equip in {x.equip_id for x in s}:
        held = np.zeros(86400, dtype=bool)
        for x in s:
            if x.equip_id == equip and x.quality == "Bad":
                i = int(x.ts.timestamp() - T0.timestamp()); held[i:i + iv[(x.equip_id, x.tag_id)]] = True
        assert 0.09 <= held.mean() <= 0.11, equip
    assert len(src.seen) == len(set(src.seen)) > 0

def test_restore_seam_has_no_violation(rhds):
    plant, rt = rhds
    a = NelsonGenerator(plant, rt, spontaneous=False)
    first = a.generate(T0, T0 + timedelta(minutes=10))
    last = {}
    for x in first:
        last.setdefault((x.equip_id, x.tag_id), []).append(x.value)
    b = NelsonGenerator(plant, rt, spontaneous=False)
    b.restore({k: v[-14:] for k, v in last.items()})
    nxt = b.generate(T0 + timedelta(minutes=10), T0 + timedelta(minutes=11))
    assert not [x for x in nxt if x.quality == "Bad"]

def test_deterministic(rhds):
    plant, rt = rhds
    run = lambda: NelsonGenerator(plant, rt).generate(T0, T0 + timedelta(minutes=3))
    assert run() == run()

def test_rfcc_driven_speed_equal(config_dir):
    plant, rt = load_plant(config_dir / "plant_rfcc.yaml"), load_runtime(config_dir / "runtime_rfcc.yaml")
    s = NelsonGenerator(plant, rt, spontaneous=False).generate(T0, T0 + timedelta(minutes=2))
    speed = lambda eq: [x.value for x in s if x.equip_id == eq and plant.tag_by_key()[(eq, x.tag_id)].signal == "speed"]
    assert speed("B-1201") == speed("E-1202") and speed("C-1401") == speed("T-1401")
```

- [ ] **Step 2: 실행해서 실패 확인** — Run: `uv run pytest tests/test_nelson_model.py -q` → FAIL

- [ ] **Step 3: `simulator/nelson_model.py` 구현** (위 생성 순서대로)

- [ ] **Step 4: 통과 확인 + 속도 측정**

Run: `uv run pytest tests/test_nelson_model.py -q` → PASS
Run: `uv run python -c "import time;from datetime import datetime,timedelta;from common.config_models import load_plant,load_runtime;from common.sim_clock import KST;from simulator.nelson_model import NelsonGenerator;p=load_plant('config/plant_rfcc.yaml');r=load_runtime('config/runtime_rfcc.yaml');t=datetime(2026,11,12,tzinfo=KST);s=time.time();NelsonGenerator(p,r).generate(t,t+timedelta(hours=1));print(time.time()-s)"`
Expected: RFCC(81 태그) 1시간이 15초 이하. 넘으면 판정·억제 표집의 핫 루프(지역 변수 바인딩, `NelsonState` 의 deque 재계산 횟수)를 줄인 뒤 다시 잰다. 결과는 커밋 메시지에 적는다.

- [ ] **Step 5: 커밋**

```bash
git add simulator/nelson_model.py tests/test_nelson_model.py
git commit -m "feat: Nelson 신호 생성기 (억제 표집 + 사건 덧씌우기 + 상태)"
```

---

### Task 6: 새 생성기로 바꾸고 옛 모델·시나리오·이력·한계값 알람 지우기

**Files:**
- Modify: `simulator/main.py` (`Simulator`, `run_mode`, `run_backfill_then_live`, 인자), `simulator/sender.py` (`recent_values`), `collector/embedded.py`, `collector/plant_context.py`, `collector/plants.py`, `collector/app.py`, `collector/jobs.py`, `collector/seed_master.py`, `collector/schema.sql`, `collector/storage.py` (`SCHEMA_VERSION = 3`, `recent_values`), `collector/settings.py`, `common/config_models.py`, `config/plants.yaml`, `config/plants.k8s.yaml`, `config/plant_*.yaml`, `docker-compose.yml`, `deploy/k8s/plant-sim-deployment.yaml`, `deploy/k8s/plant-sim-secret.example.yaml`, `tests/conftest.py` 와 남는 테스트
- Delete: `simulator/{generator,scenario,patterns,random_anomalies,random_quality,signal_model,rfcc_model}.py`, `common/scenario_models.py`, `collector/seed_history.py`, `config/{plant_hds.yaml,runtime.yaml,scenario_dev.yaml,scenario_rfcc.yaml,scenario_cdu.yaml,scenario_hou.yaml,scenario_rhds.yaml,scenario_vrds.yaml}`, `tests/{test_patterns,test_scenario,test_signal_model,test_rfcc_model,test_seed_history,test_rfcc_generator}.py`

**Interfaces:**
- Consumes: Task 5 `NelsonGenerator`, `EventSource`
- Produces:
  - `Simulator(plant, runtime, sender, clock, source: EventSource | None = None, window_sec=None, catch_up_windows=0)`
  - `run_mode(plant, runtime, sender, source: EventSource | None = None, window_sec=BACKFILL_WINDOW_SEC, on_live: Callable[[datetime], None] | None = None)` — 실시간(또는 accelerated·live 시작)으로 넘어가는 순간 `on_live(그 시각)` 을 한 번 부른다
  - `Sender.recent_values(self, count: int) -> dict[TagKey, list[float]]` (HttpSender·MemorySender 는 `{}`, LocalSender 는 생성자 인자 `recent_values` 콜러블을 부른다), 재개할 때 `generator.restore(await sender.recent_values(HISTORY))`
  - `Storage.recent_values(count: int) -> dict[TagKey, list[float]]` (태그별 최근 count 개, 오래된 것부터)
  - `TagConfig.control: ControlLimits` 필수. `TagConfig` 에서 `normal`·`delay_samples`·`location` 삭제, `PlantMeta.signal_model`·`PlantConfig.correlations`·`EquipmentConfig.seed_key` 삭제, `PlantEntry.scenario` 삭제, `Settings.sim_scenario_dir` 삭제
  - DB v3: `equipment_tag` 에서 `delay_samples`·`location` 을 빼고 `control_mean REAL NOT NULL, control_sigma REAL NOT NULL` 추가, `maintenance_history`·`failure_history` 삭제

- [ ] **Step 1: 옛 동작을 고정한 테스트 삭제·이전**

위 Delete 목록의 테스트를 지운다. `conftest.py` 의 `plant_path`·`runtime_path` 는 `plant_rhds.yaml`·`runtime_rhds.yaml` 로 바꾸고 `scenario_path`·`rfcc_scenario_path` 픽스처를 지운다. HDS 태그 ID 를 쓰던 테스트는 RHDS ID 로, `/maintenance`·`/failures`·한계값 알람(`alarm_detail` 행 생성)을 확인하던 테스트는 지운다. `test_schema_migration.py` 에 "v2 파일은 열지 않고 알린다"를 더한다.

```python
def test_v2_file_is_refused(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE equipment_tag (x); CREATE TABLE schema_meta (version INTEGER); INSERT INTO schema_meta VALUES (2);")
    con.close()
    with pytest.raises(ConfigError, match="v2"):
        Storage(db)
```

- [ ] **Step 2: 실행해서 실패 확인**

Run: `uv run pytest -q`
Expected: FAIL (v2 가 아직 허용됨, 옛 모듈 import 오류 등)

- [ ] **Step 3: 바꾸고 지운다**

- `Simulator` 는 `NelsonGenerator(plant, runtime, source)` 를 쓴다. `run_backfill_then_live` 는 재개할 때 restore 하고, 실시간 전환 때 `on_live` 를 부른다. `--scenario`·`SCENARIO_CONFIG` 를 지운다.
- `collector/embedded.py`: 시나리오 경로 처리(`sim_scenario_paths`)를 지운다. `LocalSender(ctx.receive_local, ctx.storage.sim_now, limiter, recent_values=ctx.storage.recent_values)`.
- `jobs.py` 는 집계·보관만 남기고 `_evaluate_alarms`·`SEVERITY`·`_exceed_pct` 를 지운다(`alarm_detail` 테이블과 `/alarms` 라우트는 Task 8 에서 바꾼다).
- `/maintenance`·`/failures` 라우트, `seed_history` 호출, `PlantContext.open` 의 scenario 분기를 지운다.
- 구성안 yaml 에서 `normal:`·`delay_samples:`·`location:` 항목, 설비의 `seed_key`, `signal_model`, `correlations` 를 지운다(scratchpad 스크립트, 커밋 안 함).
- compose 의 `--scenario …`, k8s `plant-sim-scenarios` 볼륨·Secret 예시, `SIM_SCENARIO_DIR` 문서 주석을 지운다. `test_k8s_manifests.py` 를 맞춘다.

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest -q` → PASS
Run: `grep -rnE "scenario|noise_sd|delay_samples|rfcc_model|signal_model|maintenance_history" common simulator collector config deploy docker-compose.yml` → 출력 없음

- [ ] **Step 5: 커밋**

```bash
git add -A
git commit -m "refactor: 생성기를 Nelson 모델로 바꾸고 시나리오·HDS·이력·한계값 알람을 지운다 (DB v3)"
```

---

### Task 7: `quality` → `status` (메시지 v2)

**Files:**
- Modify: `common/message.py`, `collector/ingest.py`, `collector/schema.sql`, `collector/storage.py`, `collector/plant_context.py`, `collector/jobs.py`, `collector/static/index.html`, `simulator/nelson_model.py`, 관련 테스트(`test_message.py`, `test_collector_ingest.py`, `test_jobs.py` 등)

**Interfaces:**
- Produces: `SCHEMA_VERSION = "2.0"`, `Status = Literal["Good", "Bad"]`, `Sample(equip_id, tag_id, ts, value: float, status: Status)`. `ts_raw.status`, `tag_current.status`. `/current` 행의 `status` 는 `NoData` | `Good` | `Bad`(`quality` 키 없음). 집계 `status`: Bad 표본이 있으면 `Bad`, 아니면 `Good`, 표본이 없으면 `NoData`.

- [ ] **Step 1: 실패하는 테스트 작성** (`tests/test_message.py`, `tests/test_collector_ingest.py`, `tests/test_jobs.py` 에 추가)

```python
def test_sample_requires_value_and_status():
    with pytest.raises(ValidationError):
        Sample(equip_id="P-3101", tag_id="VI-A-P3101", ts=TS, value=None, status="Good")
    with pytest.raises(ValidationError):
        Sample(equip_id="P-3101", tag_id="VI-A-P3101", ts=TS, value=1.0, quality="Good")

def test_v1_batch_is_rejected(client):
    body = batch_body() | {"schema_version": "1.0"}
    assert client.post("/plant/ingest", json=body).status_code == 400

def test_bucket_status_bad_when_any_bad(tmp_path, plant_path, runtime_path):
    T = datetime(2026, 11, 12, tzinfo=timezone.utc)
    plant = load_plant(plant_path); st = Storage(tmp_path / "j.db"); seed_master(st, plant)
    vals = [(33.1, "Good"), (33.2, "Good"), (40.0, "Bad")]
    st.store_batch("b1", "T", to_db(T), [("C-3101", "XI-X-C3101", to_db(T + timedelta(seconds=i)), v, s)
                                         for i, (v, s) in enumerate(vals)])
    JobRunner(st, plant, load_runtime(runtime_path))._aggregate("ts_10m", T, T + timedelta(minutes=10))
    r = st.query_dicts("SELECT status, count, avg FROM ts_10m WHERE tag_id = 'XI-X-C3101'")[0]
    assert (r["status"], r["count"]) == ("Bad", 3) and r["avg"] == pytest.approx((33.1 + 33.2 + 40.0) / 3)
```

(`batch_body`·`client`·`TS` 는 기존 테스트 파일의 도우미를 쓴다.)

- [ ] **Step 2: 실행해서 실패 확인** — Run: `uv run pytest -q` → FAIL

- [ ] **Step 3: 이름을 바꾸고 의미를 맞춘다**

디버깅 화면 열 이름은 "상태"로 하고 `Bad` 는 기존 `--bad` 색으로 강조한다. 값은 늘 있으므로 흐리게 표시하는 처리는 지운다.

- [ ] **Step 4: 통과 확인** — Run: `uv run pytest -q` → PASS, `grep -rn "quality" common simulator collector` → 출력 없음

- [ ] **Step 5: 커밋**

```bash
git add -A
git commit -m "feat: quality 를 설비 상태 status(Good/Bad)로 바꾼다 (메시지 v2)"
```

---

### Task 8: 수집 서버 상시 판정과 `/alarms`

**Files:**
- Create: `collector/alarms.py`
- Modify: `collector/schema.sql` (`alarm_detail` 삭제, `nelson_alarm` 추가), `collector/plant_context.py` (`receive` 에서 호출), `collector/app.py` (`/alarms`)
- Test: `tests/test_collector_alarms.py`

**Interfaces:**
- Consumes: Task 1 `judge`, `zscore`, `HISTORY`, `bit`. Task 6 `Storage.recent_values`.
- Produces:
  - 테이블 `nelson_alarm(alarm_id INTEGER PRIMARY KEY AUTOINCREMENT, equip_id TEXT NOT NULL, tag_id TEXT NOT NULL, rule INTEGER NOT NULL, direction TEXT, occurred_at TEXT NOT NULL, cleared_at TEXT, last_hit_at TEXT NOT NULL, hit_count INTEGER NOT NULL, value_at_occurrence REAL NOT NULL)` + `occurred_at`·`cleared_at` 인덱스
  - `CLOSE_AFTER = 15`
  - `class AlarmTracker: __init__(self, storage: Storage, plant: PlantConfig)`, `observe(self, rows: list[tuple[str, str, str, float, str]]) -> int` — 행은 `(equip_id, tag_id, ts_db, value, status)`, 돌려주는 값은 받은 `status` 와 판정이 다른 표본 수
  - `PlantContext.alarms: AlarmTracker`. `receive` 는 중복 묶음이 아닐 때 락 안에서 `observe` 를 부르고, 불일치가 있으면 `log.warning("plant %s: status mismatch on %d samples", key, n)`
  - `GET /{공장}/alarms?equip_id=&rule=&from=&to=&open=`: `rule` 은 1~8(벗어나면 422), `from`/`to` 는 겹침(`occurred_at < to AND (cleared_at IS NULL OR cleared_at >= from)`), `open=true` 면 `cleared_at IS NULL`. 정렬은 `occurred_at DESC`. 응답 키는 테이블 컬럼 그대로.

`observe`: 태그별로 시각 순 정렬하고, 그 태그의 마지막 판정 시각 이하인 행은 건너뛴다. `judge(z, tail)` 로 비트를 구한다. 규칙마다 열린 묶음이 없고 비트가 있으면 연다(`direction` 은 규칙 1·2·3·5·6 이면 `low` 비트로 "lo"/"hi", 나머지 null). 열려 있으면 `hit_count`·`last_hit_at` 을 갱신하고 비트가 없는 표본 수를 센다. 연속 15개가 되면 닫는다. 시작할 때 꼬리는 `storage.recent_values(HISTORY)` 를 z 로 바꿔 채우고, 열린 묶음은 `cleared_at IS NULL` 행에서 읽는다.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
from datetime import datetime, timedelta, timezone
from common.nelson import QUIET
from common.message import IngestBatch, Sample
from collector.app import create_single_app
from fastapi.testclient import TestClient

MU, SIG, T0 = 33.1, 0.8249781141878183, datetime(2026, 11, 12, tzinfo=timezone.utc)

def post(client, zs, start=0, status="Good"):
    samples = [Sample(equip_id="C-3101", tag_id="XI-X-C3101", ts=T0 + timedelta(seconds=start + i),
                      value=round(MU + SIG * z, 4), status=status) for i, z in enumerate(zs)]
    batch = IngestBatch(source_id="T", batch_id=f"b{start}", sent_at=T0, samples=samples)
    assert client.post("/plant/ingest", content=batch.model_dump_json(), headers={"content-type": "application/json"}).status_code == 200

def rows(client, **q):
    return client.get("/plant/alarms", params=q).json()

def test_rule1_opens_and_closes(rhds_client):
    post(rhds_client, list(QUIET) * 4 + [3.5, 3.5, 3.5] + list(QUIET) * 3)
    r = [a for a in rows(rhds_client) if a["rule"] == 1]
    assert len(r) == 1 and r[0]["hit_count"] == 3 and r[0]["direction"] == "hi"
    assert r[0]["occurred_at"].startswith("2026-11-12T00:00:32") and r[0]["cleared_at"].startswith("2026-11-12T00:00:35")
    assert set(r[0]) == {"alarm_id", "equip_id", "tag_id", "rule", "direction", "occurred_at", "cleared_at",
                         "last_hit_at", "hit_count", "value_at_occurrence"}

def test_alarms_overlap_window(rhds_client):
    post(rhds_client, list(QUIET) * 4 + [3.5] + list(QUIET) * 3)          # 00:00:32 열림, 00:00:33 닫힘
    post(rhds_client, [3.5] * 3, start=100)                               # 00:01:40 열림, 아직 열림
    in_window = rows(rhds_client, **{"from": "2026-11-12T00:01:50Z", "to": "2026-11-12T00:05:00Z"})
    assert [a["occurred_at"][:19] for a in in_window if a["rule"] == 1] == ["2026-11-12T00:01:40"]

def test_rule_filter_validated(rhds_client):
    assert rhds_client.get("/plant/alarms", params={"rule": 9}).status_code == 422

def test_open_alarm_survives_restart(rhds_db_path, rhds_app_factory):
    with TestClient(rhds_app_factory(rhds_db_path)) as c:
        post(c, list(QUIET) * 4 + [3.5, 3.5])
    with TestClient(rhds_app_factory(rhds_db_path)) as c:
        post(c, [3.5], start=34)
        r = [a for a in rows(c) if a["rule"] == 1]
        assert len(r) == 1 and r[0]["hit_count"] == 3 and r[0]["cleared_at"] is None

def test_status_mismatch_is_logged_but_alarm_recorded(rhds_client, caplog):
    post(rhds_client, list(QUIET) * 4 + [3.5], status="Good")
    assert any(a["rule"] == 1 for a in rows(rhds_client))
    assert "status mismatch" in caplog.text

def test_duplicate_batch_not_counted_twice(rhds_client):
    post(rhds_client, list(QUIET) * 4 + [3.5, 3.5])
    post(rhds_client, list(QUIET) * 4 + [3.5, 3.5])
    assert [a["hit_count"] for a in rows(rhds_client) if a["rule"] == 1] == [2]
```

(`rhds_client`·`rhds_db_path`·`rhds_app_factory` 픽스처는 이 파일에 둔다: `create_single_app(plant_rhds, runtime_rhds, db)` 를 key `"plant"` 로 띄운다.)

- [ ] **Step 2: 실행해서 실패 확인** — Run: `uv run pytest tests/test_collector_alarms.py -q` → FAIL

- [ ] **Step 3: `collector/alarms.py`·스키마·라우트 구현** (위 Interfaces 대로)

- [ ] **Step 4: 통과 확인** — Run: `uv run pytest -q` → PASS

- [ ] **Step 5: 커밋**

```bash
git add -A
git commit -m "feat: 수집 서버가 받을 때마다 Nelson 8종을 판정해 /alarms 로 낸다"
```

---

### Task 9: 사건 장부와 주입·멈춤·정답지 API, 지속 고장

**Files:**
- Create: `collector/anomaly_book.py`
- Modify: `collector/schema.sql` (`anomaly_event`), `collector/plant_context.py` (`book`, `live`, `health()` 의 `live`), `collector/embedded.py` (`source`·`on_live` 연결), `collector/app.py` (라우트), `collector/settings.py` (`PERSISTENT_FAULT_INTERVAL_MIN`)
- Test: `tests/test_anomaly_book.py`, `tests/test_anomaly_api.py`

**Interfaces:**
- Consumes: Task 3 `AnomalyEvent`, `classify`, `responses`, `MAX_RECOVERY_SEC`. Task 5 `EventSource`. Task 6 `run_mode(..., source, on_live)`.
- Produces:
  - 테이블 `anomaly_event(event_id TEXT PRIMARY KEY, origin TEXT NOT NULL, equip_id TEXT NOT NULL, rule INTEGER NOT NULL, normal_change INTEGER NOT NULL, kind TEXT NOT NULL, cause TEXT NOT NULL, action TEXT NOT NULL, response_tags TEXT NOT NULL, start_at TEXT NOT NULL, scheduled_end_at TEXT, stop_requested_at TEXT)`
  - `class Busy(Exception)`
  - `class AnomalyBook` (EventSource 구현, 스레드 안전): `__init__(self, storage: Storage, plant: PlantConfig, seed: int, interval_min: float)`, `set_live(self, at: float) -> None`, `external(self, start: float, end: float) -> list[AnomalyEvent]` (지속 고장 예정 시각이 `end` 이하이면 이때 연다), `stops(self) -> dict[str, float]`, `opened(self, event: AnomalyEvent) -> None`, `inject(self, equip_id: str, rule: int, at: float) -> AnomalyEvent` (주입·지속 고장이 살아 있으면 `Busy`, 살아 있는 자동 이상은 `at` 에서 멈춤 기록), `stop(self, equip_id: str | None, at: float) -> list[str]` (`None` 이면 전체), `answer_key(self, equip_id: str | None, from_: str | None, to: str | None) -> list[dict]`
  - 정답지 행: `event_id, origin, equip_id, rule, kind, cause, action, response_tags(list), start_at, stop_requested_at, end_at` — `end_at` = `min(scheduled_end, stop)` + 그 사건 반응 태그 회복 최댓값, 열려 있으면 null. `from`/`to` 는 겹침.
  - 지속 고장: id `PF-{plant_id}-{n:04d}`, 설비는 그 시각에 주입·지속 고장이 없는 설비 중 `default_rng([seed, n])` 로 고르고, 규칙도 같은 rng 로 1~8 에서 고른다. 처음은 `live_at + interval`, 연 뒤 `+ interval`. 재시작하면 `max(live_at, 마지막 지속 고장 start) + interval`. interval 0 이면 열지 않는다.
  - 주입 id `IN-{plant_id}-{n:04d}`
  - 라우트: `POST /{공장}/anomalies` 본문 `{"equip_id": str, "rule": int}`(rule 1~8, 아니면 422) → 201 `{"event_id", "equip_id", "rule", "start_at"}`. `POST /{공장}/anomalies/{equip_id}/stop` → 200 `{"stopped": [...]}` 또는 404(모르는 설비·사건 없음). `POST /{공장}/anomalies/stop` → 200 `{"stopped": [...]}`. `GET /{공장}/anomalies?equip_id=&from=&to=`. POST 는 `require_ingest_token` → 통합 실행이 아니면 409 "embedded only" → `ctx.live` 가 아니면 503 "backfilling" → 모르는 설비 404 → `Busy` 는 409 순서로 확인한다. 시각은 `ceil(sim_now 의 epoch) + 3`.
  - `Settings.persistent_fault_interval_min: float | None` (env `PERSISTENT_FAULT_INTERVAL_MIN`), 있으면 runtime 값을 덮어쓴다.
  - `PlantContext.book: AnomalyBook`, `PlantContext.live: bool = False`, `PlantContext.set_live(self, at: float) -> None`, `health()` 에 `"live"` 키.

- [ ] **Step 1: 실패하는 테스트 작성 (장부 단위)**

```python
import pytest
from collector.anomaly_book import AnomalyBook, Busy
from collector.storage import Storage
from collector.seed_master import seed_master
from common.config_models import load_plant
from simulator.anomaly import AnomalyEvent

@pytest.fixture
def book(config_dir, tmp_path):
    plant = load_plant(config_dir / "plant_rhds.yaml")
    st = Storage(tmp_path / "b.db"); seed_master(st, plant)
    return AnomalyBook(st, plant, seed=1, interval_min=15), st, plant

def test_inject_busy_and_stop(book):
    b, _, _ = book
    e = b.inject("C-3101", 1, at=1000.0)
    assert e.origin == "injected" and e.start == 1000.0 and e.end is None
    with pytest.raises(Busy):
        b.inject("C-3101", 2, at=1010.0)
    assert b.stop("C-3101", at=1100.0) == [e.event_id]
    assert b.stops()[e.event_id] == 1100.0

def test_stop_twice_and_empty(book):
    b, _, _ = book
    assert b.stop("C-3101", at=10.0) == [] and b.stop(None, at=10.0) == []
    e = b.inject("C-3101", 3, at=20.0)
    assert b.stop(None, at=30.0) == [e.event_id] and b.stop("C-3101", at=40.0) == []

def test_inject_truncates_active_spontaneous(book):
    b, _, _ = book
    sp = AnomalyEvent("SP-C-3101-20261112-01", "spontaneous", "C-3101", 2, 900.0, 1500.0)
    b.opened(sp)
    b.inject("C-3101", 1, at=1000.0)
    row = next(r for r in b.answer_key("C-3101", None, None) if r["event_id"] == sp.event_id)
    assert row["stop_requested_at"] is not None
    assert b.stops()[sp.event_id] == 1000.0

def test_persistent_only_after_live_every_interval(book):
    b, _, _ = book
    assert [e for e in b.external(0, 5000) if e.origin == "persistent"] == []
    b.set_live(10_000.0)
    assert [e for e in b.external(10_000, 10_899) if e.origin == "persistent"] == []
    first = [e for e in b.external(10_899, 10_901) if e.origin == "persistent"]
    assert len(first) == 1 and first[0].start == 10_900.0 and first[0].end is None
    assert len([e for e in b.external(11_799, 11_801) if e.origin == "persistent"]) == 2

def test_book_reload_keeps_open_events(book, config_dir):
    b, st, plant = book
    e = b.inject("C-3101", 5, at=50.0)
    again = AnomalyBook(st, plant, seed=1, interval_min=15)
    assert [x.event_id for x in again.external(0, 100)] == [e.event_id]

def test_answer_key_fields(book):
    b, _, _ = book
    e = b.inject("C-3101", 7, at=50.0); b.stop("C-3101", at=80.0)
    row = b.answer_key(None, None, None)[0]
    assert row["kind"] == "계측 이상" and row["response_tags"] == ["XI-X-C3101"] and row["end_at"] is not None
```

- [ ] **Step 2: 실패하는 테스트 작성 (API)** — `tests/test_anomaly_api.py`

통합 실행 앱을 `Settings(with_simulators=True, ingest_token="t", time_mode="live", backfill_days=0)` 와 RHDS 하나짜리 공장 목록(`tmp_path` DB)으로 띄운다.
- `test_token_required`: 헤더 없이 POST → 403.
- `test_split_mode_conflict`: `with_simulators=False, ingest_token="t"` 앱에서 POST → 409.
- `test_backfilling_returns_503`: `ctx.live = False` 로 두고 POST → 503.
- `test_inject_then_bad_then_stop`: `/health` 의 `live` 가 true 가 될 때까지 기다린 뒤 `{"equip_id": "C-3101", "rule": 1}` → 201. 6초 안에 `/current` 의 `C-3101/XI-X-C3101` status 가 `Bad`, `/alarms?equip_id=C-3101&rule=1` 행이 생기고 그 행 키에 `kind`·`cause`·`event_id` 가 없다. `POST /plant/anomalies/C-3101/stop` → 200. 다시 멈춤 → 404. `/anomalies` 에 그 사건이 `stop_requested_at` 과 함께 있다.
- `test_unknown_equipment_and_bad_rule`: 모르는 설비 → 404, `rule: 9` → 422.
- `test_env_overrides_interval`: `PERSISTENT_FAULT_INTERVAL_MIN=0` → `Settings.from_env().persistent_fault_interval_min == 0`.

- [ ] **Step 3: 실행해서 실패 확인** — Run: `uv run pytest tests/test_anomaly_book.py tests/test_anomaly_api.py -q` → FAIL

- [ ] **Step 4: 구현**

`PlantContext.open` 이 `AnomalyBook(storage, plant, runtime.seed, interval)` 을 만든다(interval 은 Settings 덮어쓰기 반영). `run_plant_simulator` 는 `run_mode(..., source=ctx.book, on_live=lambda t: ctx.set_live(t.timestamp()))` 로 부른다. `set_live` 는 `book.set_live` 와 `live=True` 를 함께 한다. 분리 실행(시뮬레이터 단독 `simulator.main`)은 `source=None` 이다.

- [ ] **Step 5: 통과 확인** — Run: `uv run pytest -q` → PASS

- [ ] **Step 6: 커밋**

```bash
git add -A
git commit -m "feat: 주입·멈춤·전체 멈춤·정답지 API 와 15분 지속 고장"
```

---

### Task 10: 문서와 실제 실행 검증

**Files:**
- Modify: `plant-simulator-plan.md`(기획서 5·6·7장), `README.md`, `table-plan.md`(§2.3·§6)

- [ ] **Step 1: 문서 고치기**

- 기획서: 정상 신호(고정 μ·σ, 억제 표집), 이상(자동·지속·주입), 메시지 v2(`status`), Nelson 판정·`nelson_alarm`, 주입·멈춤·정답지 API, 이력·한계값 알람 삭제를 적는다.
- README: API 표에 `/anomalies` 네 개를 더하고 `/maintenance`·`/failures` 를 지운다. 메시지 예시를 `status` 로, 시나리오 절을 "자동 이상·지속 고장" 설명과 `persistent_fault_interval_min`·`PERSISTENT_FAULT_INTERVAL_MIN` 으로 바꾼다. 배포 절에 "DB v3 — PVC 의 기존 DB 파일을 지우고 다시 띄운다"를 넣는다.
- table-plan: §2.3 은 `ts_raw.status`·`tag_current.status`·`nelson_alarm`·`anomaly_event` 로, §6 은 이상 판정을 "Nelson 묶음(`nelson_alarm`) + 앞뒤 원시값"으로 바꾸고 `quality_event` 를 지운다.

- [ ] **Step 2: 전체 테스트**

Run: `uv run pytest -q`
Expected: 전부 PASS

- [ ] **Step 3: 통합 실행으로 하루 백필과 API 확인**

Run: `rm -f /tmp/nelson-*.db && sed 's#data/#/tmp/nelson-#' config/plants.yaml > /tmp/nelson-plants.yaml && INGEST_TOKEN=t uv run python -m collector.app --plants /tmp/nelson-plants.yaml --with-simulators --time-mode backfill --backfill-days 1`
- 다섯 공장의 `/health` `live` 가 모두 true 가 되기까지 걸린 시간과 프로세스 RSS 를 적는다(기존: Mac 약 7분, RSS 약 250MB).
- `curl -XPOST -H 'X-Ingest-Token: t' -H 'content-type: application/json' localhost:8000/rhds/anomalies -d '{"equip_id":"C-3101","rule":6}'` → 201. 10초 뒤 `curl 'localhost:8000/rhds/alarms?equip_id=C-3101&open=true'` 에 규칙 6 행이 있다.
- `curl -XPOST -H 'X-Ingest-Token: t' localhost:8000/rhds/anomalies/stop` → 끝낸 id 목록. 2분 뒤 그 설비 `/current` 가 모두 Good.
- `curl 'localhost:8000/rhds/anomalies'` 의 자동 이상 행으로 하루치 설비별 건수가 12 안팎인지 본다.

- [ ] **Step 4: 커밋**

```bash
git add plant-simulator-plan.md README.md table-plan.md
git commit -m "docs: 기획서·README·table-plan 을 Nelson 이상 생성·판정에 맞춘다"
```
