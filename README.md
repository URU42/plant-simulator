# plant-simulator

Skadi 앞단 데이터 생성기. 가상 공장 다섯 곳(+ 테스트용 수첨탈황)의 설비 센서 값을 끊임없이 만들어 중앙 수집 서버로 보내고,
수집 서버가 공장마다 SQLite 파일 하나에 저장·집계·알람 판정한다. Skadi 화면·backend·AI Agent 는 HTTP API 로 읽는다.

| 키(경로) | plant_id | 공장 | 설비 · 태그 | 주기 | 근거 |
| --- | --- | --- | --- | --- | --- |
| (`/hds/`) | PL-HDS | 수첨탈황 — 배포 목록에서 뺐다(RHDS·VRDS 로 대체). 구성안·테스트는 남아 있다 | 20 · 46 | 10초, 1분 묶음 | [기획서](plant-simulator-plan.md) |
| `/rfcc/` | PL-RFCC | RFCC 회전기계 | 12 · 81 | 온도 10초 · 나머지 1초 | [RFCC 설계](docs/superpowers/specs/2026-11-09-rfcc-simulator-design.md), 샘플 `rfcc-20261109/` |
| `/cdu/` | PL-CDU | 상압증류 | 7 · 46 | 온도 10초 · 나머지 1초 | [다중 공장 설계](docs/superpowers/specs/2026-11-09-multi-plant-design.md), 샘플 `cdu-sample/` |
| `/hou/` | PL-HOU | 수첨분해 | 7 · 61 | 온도 10초 · 나머지 1초 | 같은 설계, 샘플 `hou-plant-sample/` |
| `/rhds/` | PL-RHDS | 상압 잔사유 수첨탈황 | 6 · 43 | 온도 10초 · 나머지 1초 | 같은 설계, 샘플 `RHDS_VRDS_v3/` |
| `/vrds/` | PL-VRDS | 감압 잔사유 수첨탈황 | 6 · 43 | 온도 10초 · 나머지 1초 | 같은 설계, 샘플 `RHDS_VRDS_v3/` |

샘플 묶음 폴더는 git 밖이다. 거기서 옮긴 마스터 값(계층, 설비, 태그명, 설명, 단위, 정상 기준, 경고/알람/정지 상·하한, 근거)이 `config/plant_*.yaml` 에 그대로 있다.

```text
[config/plants.yaml → plant_X.yaml · runtime_X.yaml · scenario_X.yaml] × 5
        │
        ▼
[수집 서버 (FastAPI) ──┬── 통합 실행: 공장별 생성기가 같은 프로세스에서 LocalSender 로 넣는다 (--with-simulators, k8s·compose 기본)
                       └── 분리 실행: 시뮬레이터 프로세스가 HTTP POST /{공장}/ingest 로 보낸다 (compose --profile split)]
        │  공장마다 저장 · 집계 · 알람 · 보관 삭제
        ▼
  data/{공장}.db (SQLite, WAL) × 5
        ▲
[Skadi 화면 · backend · AI Agent] (HTTP API)   [GET /{공장}/ 디버깅 화면 (SSE)]
```

| 역할 | 기술 |
| --- | --- |
| 설비 시뮬레이터 | Python 3.11, asyncio, NumPy |
| 설정 파일 | YAML + Pydantic (시작 시 검증) |
| 전송 | httpx (HTTP POST). 전송부를 분리해 두어 MQTT로 교체 가능 |
| 중앙 수집 서버 | FastAPI + Uvicorn. 공장 목록 파일 하나로 여러 공장을 `/{공장}/…` 아래에 같은 모양으로 서빙. `--with-simulators` 면 생성기까지 한 프로세스 |
| 저장 | SQLite (WAL 모드), 공장마다 파일 하나 |
| 집계·알람 배치 | 수집 서버 안에서 측정 시각 기준으로 실행 (별도 스케줄러 없음) |
| 실시간 화면 갱신 | SSE (`GET /{공장}/stream`). 디버깅용 현재값 페이지 `GET /{공장}/` 는 의존성 없는 HTML 한 장 |
| 실행 | 스크립트, Docker Compose, k8s(`deploy/k8s`, 파드 하나 500m/1Gi) |

## 빠른 시작

```bash
uv sync            # 의존성 설치 (Python 3.11+, uv)
uv run pytest      # 테스트
```

가장 간단한 실행은 통합 실행이다. 수집 서버가 공장 목록의 다섯 공장을 열고 생성기까지 같은 프로세스에서 돌린다.

```bash
uv run python -m collector.app --plants config/plants.yaml --with-simulators --time-mode backfill
```

분리 실행(기획서의 모양)은 터미널 1 에 수집 서버, 터미널 2~ 에 공장마다 시뮬레이터 하나.

```bash
uv run python -m collector.app --plants config/plants.yaml
```

예를 들어 CDU 는

```bash
# 백필: 지난 하루를 1시간 묶음으로 보낸 뒤 실시간으로 이어진다 (공장당 약 1~2분).
# 재시작하면 수집 서버의 마지막 측정 시각부터 이어서 보내므로 다시 채우지 않는다.
uv run python -m simulator.main --plant config/plant_cdu.yaml --runtime config/runtime_cdu.yaml \
    --scenario config/scenario_cdu.yaml --time-mode backfill

# 실시간만: 1초마다 값 생성·전송 (온도 태그는 10초마다)
uv run python -m simulator.main --plant config/plant_cdu.yaml --runtime config/runtime_cdu.yaml --scenario config/scenario_cdu.yaml
```

`runtime_X.yaml` 의 `collector_url` 이 `http://localhost:8000/cdu` 처럼 공장 접두어까지 가리킨다. 수첨탈황은 `plant_hds.yaml` · `runtime.yaml` · `scenario_dev.yaml`(백필 90일, 약 8분).

브라우저: `http://localhost:8000/` 은 공장 목록, `http://localhost:8000/cdu/` 는 그 공장의 현재값이 1초마다 갱신되는 디버깅 화면(값·품질·측정 시각만).

Docker Compose 기본은 통합 실행 컨테이너 하나(`plant-sim`). 분리 실행(수집 서버 1 + 시뮬레이터 5)은 `split` 프로필이다. DB 는 네임드 볼륨 `plant-data` 에 둔다.
macOS Docker Desktop 의 바인드 마운트(`./data`) 위에서는 SQLite WAL 파일이 손상된 적이 있어 쓰지 않는다. 파일이 필요하면 `docker compose cp plant-sim:/app/data/cdu.db ./data/` 로 꺼낸다.

```bash
docker compose up --build -d                    # 통합 실행 1 컨테이너
docker compose --profile split up --build -d    # 분리 실행 6 컨테이너
```

확인:

```bash
curl localhost:8000/health                                   # 다섯 공장의 sim_now · catching_up
curl localhost:8000/rhds/equipment                           # 계층 순서의 설비·태그·한계값
curl localhost:8000/rhds/current
curl localhost:8000/rhds/equipment/P-3101/tags/TI-B-3101/current
curl "localhost:8000/cdu/timeseries?equip_id=P-8101A&tag_id=PI-8101A&from=2026-11-08T00:00:00%2B09:00&to=2026-11-09T00:00:00%2B09:00&interval=10m"
curl "localhost:8000/hou/alarms?equip_id=P-2601"
curl -N localhost:8000/rfcc/stream
```

## 명령행 인자

| 프로세스 | 인자 | 환경 변수 | 기본값 |
| --- | --- | --- | --- |
| 수집 서버 | `--plants` | `PLANTS_CONFIG` | `config/plants.yaml` |
| 수집 서버 | `--host`, `--port` | `HOST`, `PORT` | `0.0.0.0`, `8000` |
| 수집 서버 | `--with-simulators` | `WITH_SIMULATORS` | 꺼짐. 켜면 공장 목록의 생성기를 같은 프로세스에서 돌린다 |
| 수집 서버 | `--time-mode`, `--backfill-days` | | 통합 실행에서 각 공장 runtime 값을 덮어쓴다 |
| 수집 서버 | | `ROOT_PATH` | 경로 접두어(예: `/plant-sim`). 루트 링크·리다이렉트·`/docs` 가 붙인다 |
| 수집 서버 | | `CORS_ORIGINS` | 쉼표 구분. 있으면 GET 에 CORS 헤더 |
| 수집 서버 | | `INGEST_TOKEN` | 있으면 외부 `POST /ingest` 는 헤더 `X-Ingest-Token` 필수. 통합 실행인데 없으면 외부 `/ingest` 는 닫힌다(403). 통합 실행의 생성기는 거치지 않는다. 시뮬레이터 `HttpSender` 도 같은 변수를 읽어 헤더를 붙인다 |
| 수집 서버 | | `SIM_SCENARIO_DIR` | 있으면 그 안의 `scenario_<key>.yaml` 을 그 공장의 시뮬레이터에만 더 준다(평가용, 이력에는 안 들어감) |
| 시뮬레이터 | `--plant` | `PLANT_CONFIG` | `config/plant_hds.yaml` |
| 시뮬레이터 | `--runtime` | `RUNTIME_CONFIG` | `config/runtime.yaml` |
| 시뮬레이터 | `--scenario` (여러 번 가능) | `SCENARIO_CONFIG` (쉼표 구분) | 없음 |
| 시뮬레이터 | `--time-mode` | | `runtime.yaml` 의 `time_mode` |
| 시뮬레이터 | `--collector-url` | `COLLECTOR_URL` | `runtime.yaml` 의 `collector_url` |

- 평가용 시나리오(비공개)는 분리 실행에서는 시뮬레이터에만 `--scenario` 를 한 번 더, 통합 실행에서는 `SIM_SCENARIO_DIR` 로 준다. 수집 서버의 이력 적재에는 공장 목록의 개발용 파일만 들어간다.
- Unit 단위로 나눠 돌리려면 runtime 파일을 복사해 `units: [UNIT-CDU-01]`, `source_id: SIM-CDU-1` 처럼 바꾸고 프로세스를 여러 개 띄운다. 값은 한 프로세스로 돌릴 때와 같다.

## 설정 파일

| 파일 | 내용 |
| --- | --- |
| `config/plants.yaml` | 수집 서버가 여는 공장 목록: `key`(URL 접두어), 구성안·실행·시나리오 파일, DB 파일. `plants.k8s.yaml` 은 DB 경로가 `/app/data/` 인 k8s 용 |
| `config/plant_{hds,rfcc,cdu,hou,rhds,vrds}.yaml` | 구성안: 계층, 설비 유형(`kind`), 표준 태그, 설비, 태그(설명·단위·주기·자릿수·한계값·`normal_ref`·`limit_basis`·`seed_key`·정상 신호 모델), 연관 규칙, `signal_model` |
| `config/runtime*.yaml` | 실행 설정: Unit 목록, `source_id`, 주기, 시간 모드, 가속 배율, 백필 기간, 시드, 수집 서버 주소(공장 접두어 포함), 원시 값 보관 기간 |
| `config/scenario_*.yaml` | 개발용 이상·정비·품질 이벤트, 우연한 잡음 알람·품질 저하 비율 |

### 태그를 가리키는 법

태그는 **(설비 ID, 태그명)** 복합키다. 태그명은 같은 설비 안에서만 고유하면 된다(RHDS 의 `TI-B-3101` 은 P-3101·C-3101·F-3101 에 모두 있다).
설정 파일에서 태그를 가리키는 문자열(`correlations.source/target`, 시나리오 `related[].tag_id`)은 `EQUIP/TAG`(예: `P-3101/TI-B-3101`)이거나,
공장 안에서 유일한 태그명이면 그냥 `TAG` 다. 겹치는 태그명을 그냥 적으면 어느 설비들에 있는지 알려 주며 멈춘다.

### 한계값

```yaml
limits: { warn_hi: 3.0, alarm_hi: 4.5, trip_hi: 7.1 }     # 상한만 (수첨탈황)
limits: { warn_lo: 2.02, alarm_lo: 1.62 }                # 하한만 (CDU 흡입 압력)
limits: { alarm_lo: 115, alarm_hi: 140 }                 # 양쪽 (HOU)
```

여섯 칸 모두 선택이며 자료에 있는 칸만 적는다. 상한은 10분 최대값이 넘으면, 하한은 10분 최소값이 내려가면 알람(`alarm_detail.direction` hi / lo).
심각도는 Trip → High, Alarm → Medium, Soft → Low. 한계값이 없는 태그(RFCC)는 알람 판정 대상이 아니다. `normal_ref`(정상 기준 원문)와 `limit_basis`(근거 원문)는 자료를 그대로 남긴 참고 문자열이다.

### 시나리오 이벤트

```yaml
events:
  - event_id: EV-RHDS-001
    equip_id: P-3101            # 설비. tag_id 가 공장 안에서 유일하면 생략할 수 있다
    tag_id: VI-A-3101           # 없으면 설비의 모든 태그에 적용
    pattern: swell              # ramp / step / spike / oscillation / swell / freeze / quality / gap
    start: 2026-11-01T17:46:40+09:00
    duration: 40m               # 90s / 30m / 12h / 5d (단위 하나)
    repeat_every: 7d            # (선택) 이 간격으로 반복. recovered_at 과 함께 쓸 수 없다
    magnitude: 1.5              # 값 패턴의 편차 크기 (태그 단위)
    related: [{ equip_id: P-3101, tag_id: PI-S-3101, ratio: -0.667 }]   # 함께 움직일 태그와 배율 (equip_id 생략 시 이벤트 설비에서 먼저 찾는다)
    quality: Bad                # quality 패턴일 때 (Bad / ScanOff / Uncertain)
    spread_to_driven: 0.6       # driven_by 로 이어진 피구동 설비 진동 태그에 번지는 비율
    failure_id: FL-DEV-001      # (선택) 있으면 failure_history 에 적재
    recovered_at: 2026-11-02T10:00:00+09:00
maintenance:
  - { work_order_id: WO-DEV-001, equip_id: C-1101, start: ..., end: ..., description: 베어링 교체 }
random_anomalies:
  rate_per_tag_day: 0.02        # 시나리오에 없는 작은 이상 (warn_hi 가 있는 태그만)
random_quality:                 # 우연한 계측 품질 저하 (HOU 생성기 규칙)
  uncertain_rate: 0.00003       # 1초 샘플당 Uncertain 확률 (10초 태그는 ×10)
  bad_segments_per_tag_week: 1.0
  bad_duration: [30s, 400s]
```

`swell` 은 완만히 올라가(진행률 0~0.25) 머물다(0.25~0.7) 내려오는(0.7~1) 발생·회복 모양이다. 정비 구간 동안 설비는 정지 상태(진동·압력 0 근처, 온도는 주변 온도)로 본다.
구성안 `correlations` 에 있는 쌍은 자동으로 따라 움직이므로 `related` 에 다시 적지 않는다.

## 신호 생성

모든 값은 `(seed, seed_key, 측정 시각)` 의 순수 함수다. 그래서 백필에서 실시간으로 넘어가도 이어지고,
Unit 별로 프로세스를 나눠도 같은 값이 나오고, 같은 시드로 다시 돌리면 같은 과거 데이터가 나온다.

- `hds` 모델: 정상값 = 기준값 + 하루 주기 변동 + 작은 잡음 + 천천히 떠도는 성분(상·하한 있는 랜덤 워크).
- `rfcc` 모델(`simulator/rfcc_model.py`, 다섯 공장 공통): 공정 부하(긴 주기 + OU + 하루 한 번 완만한 부하 변경)와 외기(정오 최고 + OU)를
  공장 안의 모든 설비가 공유하고 설비마다 부하 잡음을 더한다. 진동·유량·압력·차압·전류는 부하에 비례해 움직이고, 온도는 부하·외기를
  시정수 240초로 지연해 따라가며, 같은 축의 설비(`driven_by`)는 같은 속도를 쓴다. 느린 성분은 2026-01-01 KST 기준 600초 매듭을 보간하고,
  빠른 잡음은 날짜 블록(전날 잡음으로 예열)으로 만들어 어느 구간을 잘라도, 자정을 넘어도 같은 값이 나온다. 2026-01-01 이전 시각은 만들지 않는다.
  새 공장의 기준값은 샘플 데이터 첫 6시간 평균, 잡음은 첫 1시간 연속 차분에서 환산했다.

## 메시지 형식 v1

```json
{
  "schema_version": "1.0",
  "source_id": "SIM-RHDS",
  "batch_id": "b-20261109-172400-SIM-RHDS-0001",
  "sent_at": "2026-11-09T17:24:00+09:00",
  "samples": [
    { "equip_id": "P-3101", "tag_id": "TI-B-3101", "ts": "2026-11-09T17:23:50+09:00", "value": 63.4, "quality": "Good" }
  ]
}
```

품질은 `Good` / `Uncertain`(값 있음) / `Bad` / `ScanOff`(값 없음). 수집 규칙: 같은 `batch_id` 와 같은 `(equip_id, tag_id, ts)` 는 한 번만 저장한다.
마스터에 없는 (설비, 태그) 쌍·형식 오류·보관 기간 밖의 행은 거절하고 응답에 건수와 사유를 돌려준다. 시각 순서가 뒤바뀐 행도 받는다. v1.x 에서 추가된 필드는 무시하고 받는다.

## API

루트와 공장 접두어 아래의 두 층이다. 공장 접두어는 `config/plants.yaml` 의 `key`.

| 메서드 · 경로 | 용도 |
| --- | --- |
| `GET /` | 공장 목록 페이지 |
| `GET /health` | 전체 상태. 공장마다 `plant_id`, `sim_now`, `last_received_at`, `catching_up` |
| `GET /{공장}/` | 디버깅 화면. 설비별 표에 태그의 값·품질·측정 시각이 SSE 로 1초마다 갱신된다 |
| `POST /{공장}/ingest` | 시뮬레이터 묶음 수신 |
| `GET /{공장}/health` | 그 공장의 상태 |
| `GET /{공장}/equipment` | 계층 순서의 설비 목록과 태그(설명·단위·주기·자릿수·한계값·`normal_ref`·`limit_basis`) |
| `GET /{공장}/current` | 모든 태그의 현재값과 `status` |
| `GET /{공장}/equipment/{equip_id}/tags/{tag_id}/current` | 태그 현재값. 전송 주기와 태그 주기 중 긴 쪽의 3배 동안 안 들어오면 `status: NoData` |
| `GET /{공장}/timeseries?equip_id=&tag_id=&from=&to=&interval=1h` | 집계 시계열 (`10m` / `1h`) |
| `GET /{공장}/alarms?equip_id=&from=&to=` | 알람 목록 (`direction` hi / lo) |
| `GET /{공장}/maintenance?equip_id=` · `GET /{공장}/failures?equip_id=` | 정비·고장 이력 (개발용 시나리오에서 적재) |
| `GET /{공장}/stream` | SSE 로 새 값 실시간 전달 |

## DB (`data/{공장}.db`)

스키마 v2. 시각 컬럼은 모두 UTC ISO 문자열이고 태그는 `(equip_id, tag_id)` 복합키다. 배치는 컴퓨터 시계가 아니라 수신한 측정 시각의 최대값(`sim_now`)을 보고
그 시각이 지난 구간을 처리하므로, 백필에서도 실시간과 같은 경로로 집계·알람이 만들어진다.

| 테이블 | 채워지는 경로 |
| --- | --- |
| `ts_raw`, `tag_current` | 수신 즉시 |
| `ts_10m`, `ts_1h`, `alarm_detail` | 10분·매시 배치. 평균·최소·최대는 값이 있는 행(Good·Uncertain)으로 |
| `plant`, `unit_group`, `unit`, `equipment`, `equipment_type`, `equipment_alias`, `tag_std`, `equipment_tag` | 구성안에서 서버 시작 때 |
| `maintenance_history`, `failure_history` | 개발용 시나리오에서 서버 시작 때 |
| `ingest_batch`, `job_state`, `schema_meta` | 중복 확인, 배치 진행 상태, 스키마 버전 |

- **예전 DB 파일(v1, 태그 ID 단일 키)은 열 수 없다.** 수집 서버가 파일 경로와 함께 옮기거나 지우라고 알리고 멈춘다. `data/old/` 로 옮긴 뒤 백필을 다시 돌린다.
- 보관: 10분 배치가 끝날 때마다 `raw_retention_days` 보다 오래된 `ts_raw` 행(측정 시각 기준)과 `ingest_batch` 행(받은 시각 기준)을 지운다. 집계는 남긴다.
- 용량(하루 백필 뒤 실측, 행당 약 180 B): rfcc 1.1 GB · hou 0.7 GB · cdu 0.6 GB · rhds 0.5 GB · vrds 0.5 GB. 다섯 1초 공장을 하루 보관하면 합계 약 3.5 GB 에서 머문다. SQLite 파일은 지워도 줄지 않고 최고점에서 고정된다. 수첨탈황은 7일 보관(90일 백필 뒤 약 0.5 GB).
- 수집 서버는 프로세스 하나다. 통합 실행에서는 동시에 저장하는 공장을 둘로 제한하고(나머지는 기다린다) 값 계산·저장을 워커 스레드에서 돌린다. 분리 실행으로 다섯 공장이 동시에 첫 백필을 하면 SQLite 쓰기가 직렬화돼 하루 백필이 함께 끝나기까지 약 2~5분 걸렸고, 먼저 실시간에 들어간 공장의 1초 전송은 재시도로 버틴다.

## k8s 배포 (`deploy/k8s`)

네임스페이스 `u4-2`(CPU 2 · 4Gi 를 frontend·backend·agent 와 공유)에 **파드 하나**로 올린다. 수집 서버가 다섯 공장의 생성기를 같은 프로세스에서 돌린다.

| 항목 | 값 |
| --- | --- |
| 자원 | requests 200m/512Mi, limits 500m/1Gi |
| 스토리지 | PVC `plant-sim-data` 20Gi(gp3, RWO). 다섯 공장 SQLite, 하루 보관 약 3.5 GB 에서 고정 |
| 외부 | Ingress `https://u4-2.skala-us.com/plant-sim/…` (접두어 제거, SSE 용 버퍼링 off·타임아웃 3600s). 조회·화면·SSE |
| 내부 | `http://plant-sim.u4-2.svc.cluster.local:8000/{공장}/…` (접두어 없음, CORS 불필요) |
| 쓰기 보호 | 통합 실행에서 외부 `POST /ingest` 는 기본적으로 닫혀 있다(403). 열려면 Secret `plant-sim` 의 `ingest-token` 을 만들고 보내는 쪽이 `X-Ingest-Token` 을 붙인다 |
| 프로브 | startupProbe 30초×60회(첫 백필 30분까지), readiness 10초, liveness 30초. 값 계산·저장은 워커 스레드에서 돌고 조회 핸들러는 스레드풀에서 돌아 백필 중에도 응답한다. 생성기 태스크가 죽으면 `/health` 가 503 이 되어 liveness 가 다시 띄운다 |

```bash
# 1. 이미지 (PC 에서; Harbor 가 인터넷에서 닿으면 .github/workflows/build-image.yml 로도 가능)
docker build --platform linux/amd64 -t <HARBOR>/u4-2/plant-simulator:<TAG> . && docker push <HARBOR>/u4-2/plant-simulator:<TAG>
# 2. deploy/k8s/kustomization.yaml 의 images 를 그 주소·태그로 바꾼다
# 3. Secret (선택): kubectl -n u4-2 create secret generic plant-sim --from-literal=ingest-token='<긴 문자열>'
#    평가용 시나리오 (선택): kubectl -n u4-2 create secret generic plant-sim-scenarios --from-file=scenario_cdu.yaml=<파일>
# 4. 적용·확인
kubectl apply -k deploy/k8s -n u4-2
kubectl -n u4-2 rollout status deploy/plant-sim
curl https://u4-2.skala-us.com/plant-sim/health
```

- 첫 기동은 다섯 공장 하루치 백필이다. CPU 500m 에서 수 분~십수 분 걸리며 그동안 `/health` 의 `sim_now` 가 전진한다.
- 백필 기간을 늘리려면(`--backfill-days`) PVC 의 DB 파일을 지우고 다시 채워야 한다. 재개는 마지막 시각부터만 이어 간다.
- 다른 파드는 DB 파일을 직접 읽을 수 없다(RWO). HTTP API(`/current`, `/timeseries`, `/alarms`, `/maintenance`, `/failures`, `/stream`)를 쓴다.
- 실측(로컬 Mac, 다섯 공장 통합 실행): 백필 중 `/health` 응답 0.1초 이하(생성·저장이 워커 스레드에서 돌아 루프가 비어 있다), 다섯 공장 하루 백필 약 7분, 백필 뒤 Python 프로세스 RSS 약 250 MB(`docker stats` 는 SQLite 파일 캐시까지 세어 약 740 MiB 로 보인다. 캐시는 회수 가능하다). CPU 500m 에서는 백필이 2~3배 더 걸린다.

## 폴더

```text
config/      plants.yaml · plant_*.yaml · runtime*.yaml · scenario_*.yaml
common/      config_models(복합키·TagRef·한계값) · scenario_models · message(v1) · sim_clock · timefmt
simulator/   signal_model(hds) · rfcc_model · patterns · scenario · random_anomalies · random_quality · generator · sender · main
collector/   app(라우트) · plants(공장 목록) · plant_context(공장별 문맥·수신 핵심) · embedded(통합 실행) · settings · ingest · storage · schema.sql · jobs · seed_master · seed_history · stream · static/index.html
deploy/k8s/  deployment · service · pvc · ingress · secret.example · kustomization
docs/        superpowers/specs (RFCC · 다중 공장 설계) · superpowers/plans (구현 계획)
tests/       기획서 9.2 단계별 완료 기준 + 설계 §5 테스트
```
