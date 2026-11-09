# plant-simulator

Skadi 앞단 데이터 생성기. 가상 공장 설비의 센서 값을 끊임없이 만들어 중앙 수집 서버로 보내고,
수집 서버가 SQLite 파일 하나에 저장·집계·알람 판정한다. Skadi 화면과 AI Agent는 이 DB를 읽기만 한다.

구성안은 두 개다. 코드는 같고 설정 파일만 다르다.

| 구성안 | 설비 · 태그 | 주기 | 근거 문서 |
| --- | --- | --- | --- |
| **RFCC 회전기계** (`plant_rfcc.yaml`, 기본) | 12대 · 81개 | 온도 16개 10초, 나머지 65개 1초 | [설계 문서](docs/superpowers/specs/2026-11-09-rfcc-simulator-design.md). 샘플 자료 `rfcc-20261109/`(git 밖)의 생성기를 옮겼다 |
| 수첨탈황 (`plant_hds.yaml`) | 20대 · 46개 | 10초, 1분 묶음 전송 | [기획서](plant-simulator-plan.md) |

```text
[config/plant_rfcc.yaml · runtime_rfcc.yaml · scenario_rfcc.yaml]
        │
        ▼
[설비 시뮬레이터]  ── HTTP POST /ingest (1초 묶음) ──▶  [중앙 수집 서버 (FastAPI)]
  1초·10초마다 값 생성                                      │  저장 · 집계 · 알람 배치 · 보관 삭제
                                                            ▼
                                                   data/rfcc.db (SQLite, WAL)
                                                            ▲
                                  [Skadi 화면 · AI Agent] (읽기만)   [GET / 디버깅 화면 (SSE)]
```

| 역할 | 기술 |
| --- | --- |
| 설비 시뮬레이터 | Python 3.11, asyncio, NumPy |
| 설정 파일 | YAML + Pydantic (시작 시 검증) |
| 전송 | httpx (HTTP POST). 전송부를 분리해 두어 MQTT로 교체 가능 |
| 중앙 수집 서버 | FastAPI + Uvicorn |
| 저장 | SQLite (WAL 모드), 파일 하나 |
| 집계·알람 배치 | 수집 서버 안에서 측정 시각 기준으로 실행 (별도 스케줄러 없음) |
| 실시간 화면 갱신 | SSE (`GET /stream`). 디버깅용 현재값 페이지 `GET /` 는 의존성 없는 HTML 한 장 |
| 실행 | 스크립트 또는 Docker Compose (`restart: always`) |

## 빠른 시작

```bash
uv sync            # 의존성 설치 (Python 3.11+, uv)
uv run pytest      # 테스트
```

### RFCC 구성안 (기본)

터미널 1: 수집 서버 (설비 마스터를 적재하고 8000 포트로 뜬다)

```bash
uv run python -m collector.app --plant config/plant_rfcc.yaml --runtime config/runtime_rfcc.yaml \
    --scenario config/scenario_rfcc.yaml --db data/rfcc.db
```

터미널 2: 시뮬레이터

```bash
# 백필: 지난 하루를 1시간 묶음(약 24만 샘플)으로 보낸 뒤 실시간으로 이어진다 (약 70초).
# 재시작하면 수집 서버의 마지막 측정 시각부터 이어서 보내므로 다시 채우지 않는다.
uv run python -m simulator.main --plant config/plant_rfcc.yaml --runtime config/runtime_rfcc.yaml \
    --scenario config/scenario_rfcc.yaml --time-mode backfill

# 실시간: 1초마다 값 생성·전송 (온도 태그는 10초마다)
uv run python -m simulator.main --plant config/plant_rfcc.yaml --runtime config/runtime_rfcc.yaml \
    --scenario config/scenario_rfcc.yaml
```

브라우저에서 `http://localhost:8000/` 을 열면 설비 12대·태그 81개의 현재값이 1초마다 갱신된다(디버깅용, 값·품질·측정 시각만).

Docker Compose 로 두 프로세스를 함께 (기본이 RFCC + 백필):

```bash
docker compose up --build -d
```

### 수첨탈황 구성안

```bash
uv run python -m collector.app --scenario config/scenario_dev.yaml --db data/plant.db
uv run python -m simulator.main --scenario config/scenario_dev.yaml --time-mode backfill   # 90일 백필 후 실시간 (약 8분)
uv run python -m simulator.main --scenario config/scenario_dev.yaml --time-mode accelerated   # 시계를 speed 배로

PLANT_CONFIG=config/plant_hds.yaml RUNTIME_CONFIG=config/runtime.yaml SCENARIO_CONFIG=config/scenario_dev.yaml \
    DB_PATH=data/plant.db docker compose up --build -d
```

### 확인

```bash
curl localhost:8000/health
curl localhost:8000/equipment
curl localhost:8000/current
curl localhost:8000/tags/VI-P-1101A/current
curl "localhost:8000/timeseries?tag_id=VI-P-1101A&from=2026-11-08T00:00:00%2B09:00&to=2026-11-09T00:00:00%2B09:00&interval=10m"
curl "localhost:8000/alarms?equip_id=C-1101"      # 수첨탈황 구성안만 알람이 있다
curl -N localhost:8000/stream
```

## 명령행 인자

설정 파일 경로는 `runtime.yaml` 항목이 아니라 인자(또는 환경 변수)로 준다.

| 프로세스 | 인자 | 환경 변수 | 기본값 |
| --- | --- | --- | --- |
| 둘 다 | `--plant` | `PLANT_CONFIG` | `config/plant_hds.yaml` (compose 는 `plant_rfcc.yaml`) |
| 둘 다 | `--runtime` | `RUNTIME_CONFIG` | `config/runtime.yaml` (compose 는 `runtime_rfcc.yaml`) |
| 수집 서버 | `--scenario` (개발용만) | `SCENARIO_CONFIG` | 없음 |
| 수집 서버 | `--db`, `--host`, `--port` | `DB_PATH`, `HOST`, `PORT` | `data/plant.db`, `0.0.0.0`, `8000` |
| 시뮬레이터 | `--scenario` (여러 번 가능) | `SCENARIO_CONFIG` (쉼표 구분) | 없음 |
| 시뮬레이터 | `--time-mode` | | `runtime.yaml` 의 `time_mode` |
| 시뮬레이터 | `--collector-url` | `COLLECTOR_URL` | `runtime.yaml` 의 `collector_url` |

- 평가용 시나리오(비공개)는 시뮬레이터에만 `--scenario` 를 한 번 더 주어 함께 적용한다. 수집 서버에는 개발용 파일만 준다.
- Unit 단위로 나눠 돌리려면 `runtime.yaml` 을 복사해 `units: [U-1]`, `source_id: SIM-U1` 처럼 바꾸고 프로세스를 여러 개 띄운다. 값은 한 프로세스로 돌릴 때와 같다.
- 구성안을 바꾸려면 `--plant` 에 다른 파일을 주고, 다른 DB 파일을 쓰거나 DB 를 비운 뒤 백필을 다시 돌린다. 구성안마다 설비 마스터가 다르므로 DB 파일을 같이 쓰지 않는다.

## 설정 파일

| 파일 | 내용 | 바뀌는 때 |
| --- | --- | --- |
| `config/plant_rfcc.yaml` | RFCC 구성안: 계층(Unit Group 5, Unit 8), 설비 유형 5, 표준 태그 14종, 설비 12대, 태그 81개(설명·주기·자릿수·정상 신호 모델·`seed_key`). 한계값 없음 | 공장 구성을 바꿀 때 |
| `config/runtime_rfcc.yaml` | RFCC 실행 설정: 1초 생성·전송, 백필 1일, 보관 1일, 시드 61109 | 실행 방식을 바꿀 때 |
| `config/scenario_rfcc.yaml` | RFCC 이상 징후 2건(매주 반복)과 센서 품질 Bad 60초 | 개발 중 수시로 |
| `config/plant_hds.yaml` | 수첨탈황 구성안: 설비 20대, 태그 46개(한계값·정상 신호 모델·`seed_key`), 연관 규칙 | 공장 구성을 바꿀 때 |
| `config/runtime.yaml` | 수첨탈황 실행 설정: 10초 생성, 1분 전송, 백필 90일, 보관 7일 | 실행 방식을 바꿀 때 |
| `config/scenario_dev.yaml` | 수첨탈황 개발용 이상·정비 이벤트, 우연한 잡음 알람 비율 | 개발 중 수시로 |

구성안 공통 필드 외에 RFCC 를 위해 추가된 것: `plant.signal_model`(`hds` / `rfcc`, 값을 만드는 식을 고른다), 설비 유형의 `kind`(pump / blower / expander / compressor / turbine),
설비의 `seed_key`(rfcc 필수), 태그의 `interval_sec`(없으면 `sample_interval_sec`), `decimals`(기본 3), `description`, 그리고 `limits` 는 선택이 됐다.
한계값 없는 태그는 알람 판정과 우연한 잡음 알람에서 빠진다.

설비 ID(`C-1101`)와 태그 ID(`VI-C1101`)는 구성안 파일에만 적는다. 코드는 ID 형식을 해석하지 않으며,
시나리오·연관 규칙·`driven_by` 가 가리키는 ID 가 구성안에 없으면 시작을 멈추고 어느 ID 가 빠졌는지 알려 준다.
태그별 시드는 ID 가 아니라 `seed_key` 에서 만들므로 ID 이름을 바꿔도 생성 값은 그대로다.

### 시나리오 이벤트

```yaml
events:
  - event_id: EV-DEV-001
    tag_id: VI-C1101            # 또는 equip_id (설비의 모든 태그에 적용)
    pattern: ramp               # ramp / step / spike / oscillation / swell / freeze / quality / gap
    start: 2026-10-25T00:00:00+09:00
    duration: 6d                # 90s / 30m / 12h / 5d
    repeat_every: 7d            # (선택) 이 간격으로 같은 이벤트를 반복한다. recovered_at 과 함께 쓸 수 없다
    magnitude: 3.2              # 값 패턴의 편차 크기 (태그 단위)
    related: [{ tag_id: PI-C1101, ratio: 0.5 }]   # 함께 움직일 태그와 배율
    spread_to_driven: 0.6       # driven_by 로 이어진 피구동 설비 진동 태그에 번지는 비율
    failure_id: FL-DEV-001
    work_order_id: WO-DEV-001
    doc_id: DOC-DEV-BRG-WEAR
    recovered_at: 2026-11-02T10:00:00+09:00   # 정비 후 정상 복귀 시각. 그때까지 마지막 값을 유지
maintenance:
  - { work_order_id: WO-DEV-001, equip_id: C-1101, start: ..., end: ..., description: 베어링 교체 }
random_anomalies:
  rate_per_tag_day: 0.02        # 시나리오에 없는 작은 이상을 태그당 하루 평균 이만큼 삽입
```

진동→온도처럼 구성안 `correlations` 에 있는 쌍은 자동으로 따라 움직이므로 `related` 에 다시 적지 않는다.
정비 구간 동안 설비는 정지 상태(진동·압력 0 근처, 온도는 주변 온도)로 본다.
`swell` 은 완만히 올라가(진행률 0~0.25) 머물다(0.25~0.7) 내려오는(0.7~1) 발생·회복 모양으로, RFCC 샘플 자료의 두 이상 징후가 이 모양이다.

## 신호 생성

모든 값은 `(seed, seed_key, 측정 시각)` 의 순수 함수다. 그래서 백필에서 실시간으로 넘어가도 이어지고,
Unit 별로 프로세스를 나눠도 같은 값이 나오고, 같은 시드로 다시 돌리면 같은 과거 데이터가 나온다.

- `hds` 모델: 정상값 = 기준값 + 하루 주기 변동 + 작은 잡음 + 천천히 떠도는 성분(상·하한 있는 랜덤 워크).
- `rfcc` 모델(`simulator/rfcc_model.py`): 샘플 자료 생성기의 식을 옮겼다. 공정 부하(긴 주기 + OU + 하루 한 번 완만한 부하 변경)와
  외기(정오 최고 + OU)를 모든 설비가 공유하고, 설비마다 부하 잡음을 더한다. 진동·유량·압력·전류는 부하에 비례해 움직이고,
  온도는 부하·외기를 시정수 240초로 지연해 따라가며, 같은 축의 설비(`driven_by`)는 같은 속도를 쓴다.
  느린 성분은 2026-01-01 KST 기준 600초 매듭을 보간하고, 빠른 잡음은 날짜 블록으로 만들어 어느 구간을 잘라도 같은 값이 나온다.
  샘플 자료의 설비별 통계(평균·표준편차·자기상관)와 맞는다. 2026-01-01 이전 시각은 만들지 않는다.

## 메시지 형식 v1

```json
{
  "schema_version": "1.0",
  "source_id": "SIM-ALL",
  "batch_id": "b-20261108-172400-SIM-ALL-0001",
  "sent_at": "2026-11-08T17:24:00+09:00",
  "samples": [
    { "equip_id": "C-1101", "tag_id": "VI-C1101", "ts": "2026-11-08T17:23:10+09:00", "value": 2.34, "quality": "Good" }
  ]
}
```

수집 규칙: 같은 `batch_id` 와 같은 `(tag_id, ts)` 는 한 번만 저장한다. 모르는 태그·형식 오류·보관 기간 밖의 행은
거절하고 응답에 건수와 사유를 돌려준다. 시각 순서가 뒤바뀐 행도 받는다. v1.x 에서 추가된 필드는 무시하고 받는다.

## API

| 메서드 · 경로 | 용도 |
| --- | --- |
| `POST /ingest` | 시뮬레이터 묶음 수신 |
| `GET /` | 디버깅 화면. 설비별 표에 태그 81개의 값·품질·측정 시각이 SSE 로 1초마다 갱신된다 |
| `GET /health` | 서버 상태, 마지막 수신 시각, 시뮬레이터 현재 시각(`sim_now`), 밀린 배치 처리 중 여부(`catching_up`) |
| `GET /equipment` | 계층 순서의 설비 목록과 태그(설명·단위·주기·자릿수). 화면이 표를 그릴 때 쓴다 |
| `GET /current` | 모든 태그의 현재값과 `status` |
| `GET /tags/{tag_id}/current` | 태그 현재값. 전송 주기와 태그 주기 중 긴 쪽의 3배 동안 안 들어오면 `status: NoData` |
| `GET /timeseries?tag_id=&from=&to=&interval=1h` | 집계 시계열 (`10m` / `1h`) |
| `GET /alarms?equip_id=&from=&to=` | 알람 목록 |
| `GET /stream` | SSE 로 새 값 실시간 전달 |

## DB (`data/rfcc.db`, `data/plant.db`)

시각 컬럼은 모두 UTC ISO 문자열이다. 배치는 컴퓨터 시계가 아니라 수신한 측정 시각의 최대값(`sim_now`)을 보고
그 시각이 지난 구간을 처리하므로, 백필에서도 실시간과 같은 경로로 집계·알람이 만들어진다.

| 테이블 | 채워지는 경로 |
| --- | --- |
| `ts_raw`, `tag_current` | 수신 즉시 |
| `ts_10m`, `ts_1h`, `alarm_detail` | 10분·매시 배치. 알람 심각도는 Trip → High, Alarm → Medium, Soft → Low. 한계값 없는 태그는 알람 없음 |
| `plant`, `unit_group`, `unit`, `equipment`, `equipment_type`, `equipment_alias`, `tag_std`, `equipment_tag` | 구성안에서 서버 시작 때 |
| `maintenance_history`, `failure_history` | 개발용 시나리오에서 서버 시작 때 |
| `ingest_batch`, `job_state` | 중복 확인, 배치 진행 상태 |

보관: 10분 배치가 끝날 때마다 `raw_retention_days` 보다 오래된 `ts_raw` 행(측정 시각 기준)과 `ingest_batch` 행(받은 시각 기준)을 지운다.
집계는 남긴다. RFCC 는 1초 데이터라 하루에 약 580만 행이 쌓이며, 하루 보관이면 DB 파일은 약 1 GB 에서 머문다
(SQLite 파일은 지워도 줄지 않고 최고점에서 고정된다). 수첨탈황 구성안은 7일 보관.

## 폴더

```text
config/      plant_rfcc.yaml · runtime_rfcc.yaml · scenario_rfcc.yaml · plant_hds.yaml · runtime.yaml · scenario_dev.yaml
common/      config_models · scenario_models · message(v1) · sim_clock · timefmt
simulator/   signal_model(hds) · rfcc_model · patterns · scenario · random_anomalies · generator · sender · main
collector/   app · ingest · storage · schema.sql · jobs · seed_master · seed_history · stream · static/index.html
docs/        superpowers/specs (RFCC 설계) · superpowers/plans (구현 계획)
tests/       기획서 9.2 단계별 완료 기준 + RFCC 설계 §5 테스트
```
