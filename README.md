# plant-simulator

Skadi 앞단 데이터 생성기. 가상 공장 다섯 곳의 설비 센서 값을 끊임없이 만들어 중앙 수집 서버로 보내고,
수집 서버가 공장마다 SQLite 파일 하나에 저장·집계하며 Nelson 8종으로 알람을 판정한다. 값은 정상 신호에 이상(자동·지속 고장·주입)을 얹어 만든다. Skadi 화면·backend·AI Agent 는 HTTP API 로 읽는다.

| 키(경로) | plant_id | 공장 | 설비 · 태그 | 주기 | 근거 |
| --- | --- | --- | --- | --- | --- |
| `/rfcc/` | PL-RFCC | RFCC 회전기계 | 12 · 81 | 온도 10초 · 나머지 1초 | [RFCC 설계](docs/superpowers/specs/2026-11-09-rfcc-simulator-design.md), 샘플 `rfcc-20261109/` |
| `/cdu/` | PL-CDU | 상압증류 | 7 · 46 | 온도 10초 · 나머지 1초 | [다중 공장 설계](docs/superpowers/specs/2026-11-09-multi-plant-design.md), 샘플 `cdu-sample/` |
| `/hou/` | PL-HOU | 수첨분해 | 7 · 61 | 온도 10초 · 나머지 1초 | 같은 설계, 샘플 `hou-plant-sample/` |
| `/rhds/` | PL-RHDS | 상압 잔사유 수첨탈황 | 6 · 43 | 온도 10초 · 나머지 1초 | 같은 설계, 샘플 `RHDS_VRDS_v3/` |
| `/vrds/` | PL-VRDS | 감압 잔사유 수첨탈황 | 6 · 43 | 온도 10초 · 나머지 1초 | 같은 설계, 샘플 `RHDS_VRDS_v3/` |

샘플 묶음 폴더는 git 밖이다. 거기서 옮긴 마스터 값(계층, 설비, 태그명, 설명, 단위, 정상 기준, 경고/알람/정지 상·하한(참고값), 근거)과 Nelson 기준 μ·σ(`baselines.json`)가 `config/plant_*.yaml` 에 있다. 설계와 계획은 [기획서](plant-simulator-plan.md), [Nelson 이상 설계](docs/superpowers/specs/2026-11-12-nelson-anomaly-design.md).

```text
[config/plants.yaml → plant_X.yaml · runtime_X.yaml] × 5
        │
        ▼
[수집 서버 (FastAPI) ──┬── 통합 실행: 공장별 생성기가 같은 프로세스에서 LocalSender 로 넣는다 (--with-simulators, k8s·compose 기본)
                       └── 분리 실행: 시뮬레이터 프로세스가 HTTP POST /{공장}/ingest 로 보낸다 (compose --profile split)]
        │  공장마다 저장 · 집계 · Nelson 판정 · 보관 삭제
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
| 집계 배치 · Nelson 판정 | 집계·보관은 수집 서버 안에서 측정 시각 기준으로 실행(별도 스케줄러 없음). Nelson 판정은 받을 때마다 |
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
    --time-mode backfill

# 실시간만: 1초마다 값 생성·전송 (온도 태그는 10초마다)
uv run python -m simulator.main --plant config/plant_cdu.yaml --runtime config/runtime_cdu.yaml
```

`runtime_X.yaml` 의 `collector_url` 이 `http://localhost:8000/cdu` 처럼 공장 접두어까지 가리킨다.
분리 실행에서는 자동 이상(저절로 회복)만 나오고 주입·멈춤·지속 고장·정답지(`/anomalies`)는 쓸 수 없다.

브라우저: `http://localhost:8000/` 은 공장 목록, `http://localhost:8000/cdu/` 는 그 공장의 현재값이 1초마다 갱신되는 디버깅 화면(값·상태·측정 시각만).

Docker Compose 기본은 통합 실행 컨테이너 하나(`plant-sim`). 분리 실행(수집 서버 1 + 시뮬레이터 5)은 `split` 프로필이다. DB 는 네임드 볼륨 `plant-data` 에 둔다.
macOS Docker Desktop 의 바인드 마운트(`./data`) 위에서는 SQLite WAL 파일이 손상된 적이 있어 쓰지 않는다. 파일이 필요하면 `docker compose cp plant-sim:/app/data/cdu.db ./data/` 로 꺼낸다.

```bash
docker compose up --build -d                    # 통합 실행 1 컨테이너
docker compose --profile split up --build -d    # 분리 실행 6 컨테이너
INGEST_TOKEN=<긴 문자열> PERSISTENT_FAULT_INTERVAL_MIN=0 docker compose up -d   # 주입·멈춤을 쓰려면 토큰, 지속 고장을 끄려면 0
```

**DB v3 로 올릴 때(이전 버전의 볼륨이 있으면)**: 이전 DB 파일은 열리지 않으므로 볼륨을 지우고 다시 띄운다. `docker compose down -v` (또는 `docker volume rm <프로젝트>_plant-data`).

확인:

```bash
curl localhost:8000/health                                   # 다섯 공장의 sim_now · catching_up
curl localhost:8000/rhds/equipment                           # 계층 순서의 설비·태그·한계값
curl localhost:8000/rhds/current
curl localhost:8000/rhds/equipment/P-3101/tags/TI-B-P3101/current
curl "localhost:8000/cdu/timeseries?equip_id=P-8101A&tag_id=PI-8101A&from=2026-11-08T00:00:00%2B09:00&to=2026-11-09T00:00:00%2B09:00&interval=10m"
curl "localhost:8000/hou/alarms?equip_id=P-2601&open=true"          # 열려 있는 Nelson 알람
curl localhost:8000/rhds/anomalies                           # 정답지(원인·분류·조치·반응 태그)
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
| 수집 서버 | | `INGEST_TOKEN` | 있으면 외부 `POST /ingest` 와 `POST /anomalies…`(주입·멈춤)는 헤더 `X-Ingest-Token` 필수. 통합 실행인데 없으면 둘 다 닫힌다(403). 통합 실행의 생성기는 거치지 않는다. 시뮬레이터 `HttpSender` 도 같은 변수를 읽어 헤더를 붙인다 |
| 수집 서버 | | `PERSISTENT_FAULT_INTERVAL_MIN` | 있으면 모든 공장의 지속 고장 간격(분)을 덮어쓴다. 0 이면 끈다. 0 이상의 수가 아니면 시작하지 않는다 |
| 수집 서버 | | `PERSISTENT_FAULT_MAX_OPEN` | 있으면 모든 공장의 동시 지속 고장 상한(기본 3)을 덮어쓴다. 1 이상의 정수가 아니면 시작하지 않는다 |
| 시뮬레이터 | `--plant` | `PLANT_CONFIG` | `config/plant_rhds.yaml` |
| 시뮬레이터 | `--runtime` | `RUNTIME_CONFIG` | `config/runtime_rhds.yaml` |
| 시뮬레이터 | `--time-mode` | | `runtime.yaml` 의 `time_mode` |
| 시뮬레이터 | `--collector-url` | `COLLECTOR_URL` | `runtime.yaml` 의 `collector_url` |

- Unit 단위로 나눠 돌리려면 runtime 파일을 복사해 `units: [UNIT-CDU-01]`, `source_id: SIM-CDU-1` 처럼 바꾸고 프로세스를 여러 개 띄운다. 값은 한 프로세스로 돌릴 때와 같다.

## 설정 파일

| 파일 | 내용 |
| --- | --- |
| `config/plants.yaml` | 수집 서버가 여는 공장 목록: `key`(URL 접두어), 구성안·실행 파일, DB 파일. `plants.k8s.yaml` 은 DB 경로가 `/app/data/` 인 k8s 용 |
| `config/plant_{rfcc,cdu,hou,rhds,vrds}.yaml` | 구성안: 계층, 설비 유형(`kind`), 표준 태그, 설비, 태그(설명·단위·주기·자릿수·한계값(참고)·`normal_ref`·`limit_basis`·`seed_key`·`control` 의 μ·σ) |
| `config/runtime*.yaml` | 실행 설정: Unit 목록, `source_id`, 주기, 시간 모드, 가속 배율, 백필 기간, 시드, 수집 서버 주소(공장 접두어 포함), 원시 값 보관 기간, `persistent_fault_interval_min`(지속 고장 간격, 분), `persistent_fault_max_open`(동시 지속 고장 상한) |

### 태그를 가리키는 법

태그는 **(설비 ID, 태그명)** 복합키다. 다섯 공장의 태그명은 공장 안에서 고유하다(RHDS·VRDS 는 자료에서 겹치던 `TI-B-3101` 등에 설비 종류 문자를 넣어 `TI-B-P3101`·`TI-B-C3101`·`TI-B-F3101` 로 바꿨다).
설정 파일에서 태그를 가리키는 문자열(`driven_by` 짝 등)은 `EQUIP/TAG`(예: `P-3101/TI-B-P3101`)이거나,
공장 안에서 유일한 태그명이면 그냥 `TAG` 다. 겹치는 태그명을 그냥 적으면 어느 설비들에 있는지 알려 주며 멈춘다.

### 한계값과 Nelson 기준

```yaml
limits: { warn_hi: 3.0, alarm_hi: 4.5, trip_hi: 7.1 }     # 자료의 참고값(상한만)
control: { mean: 2.31, sigma: 0.08 }                       # Nelson 기준 μ·σ — 생성과 판정이 쓰는 값
```

`limits` 여섯 칸은 자료에 있는 것만 적는 참고값으로 마스터(`/equipment`)에 남기고 **알람에는 쓰지 않는다**. 알람은 태그별 고정 `control` μ·σ 에 대한 Nelson 8종뿐이다.
RFCC 는 `limits` 를 μ ± 3σ 로 다시 계산해 두었다(`limit_basis`). `normal_ref`·`limit_basis` 는 자료 원문 참고 문자열이다. 값은 구성안의 `decimals` 로 반올림한 뒤 판정한다.

### 이상 생성: 자동 이상 · 지속 고장 · 주입

이상 사건 = (설비, Nelson 규칙 1~8, 시작, 끝). 모양·반응 태그·분류(기계/계측/제어 이상)는 data-v3 와 같다. 세 가지로 생긴다.

| 종류 | 언제 | 끝 |
| --- | --- | --- |
| 자동 이상 (`spontaneous`) | 설비·KST 날짜마다 일정표(시드 고정)로 하루 12건 안팎. 설비당 이상 시간이 약 10%. 백필 구간에도 생긴다 | 정해진 길이(200~1800초)가 지나면 저절로 회복 |
| 지속 고장 (`persistent`) | 실시간에 들어간 뒤 `persistent_fault_interval_min`(기본 15분)마다 놀고 있는 설비 하나에 무작위 규칙. 환경 변수 `PERSISTENT_FAULT_INTERVAL_MIN` 이 덮어쓰고 0 이면 끈다 | 멈춤 API 를 부를 때까지 |
| 주입 (`injected`) | `POST /{공장}/anomalies` | 멈춤 API 를 부를 때까지 |

- 같은 설비에 지속 고장·주입이 살아 있으면 겹치는 자동 이상은 건너뛴다. 새 지속 고장이 열리면 그 설비의 자동 이상은 끝난다. 놀고 있는 설비가 없으면 그 회차는 건너뛴다.
- 우연한 잡음 위반은 그대로 둔다(정상 신호는 억제 표집으로 거의 깨끗하다). 결측·품질 저하·정비 정지는 없다.

### 이상 API (통합 실행 전용)

| 요청 | 동작 |
| --- | --- |
| `POST /{공장}/anomalies` `{"equip_id","rule"}` | 시뮬레이터 시각 기준 `ceil(sim_now)+3`초 첫 표본부터 사건 시작. 201 + `{event_id, equip_id, rule, start_at}` |
| `POST /{공장}/anomalies/{equip_id}/stop` | 그 설비의 살아 있는 사건(주입·지속 고장·자동 이상)을 `ceil(sim_now)+3`초에 끝내고 회복. 200 + `{"stopped": [event_id…], "stop_requested_at"}`. 사건이 없으면 404 |
| `POST /{공장}/anomalies/stop` | 공장의 살아 있는 사건 전부를 같은 방식으로 끝낸다. 200 + `{"stopped": [event_id…], "stop_requested_at"}`(없으면 빈 목록). 앞으로의 자동 이상·지속 고장 일정은 이어진다 |
| `GET /{공장}/anomalies?equip_id=&from=&to=` | 정답지: `event_id`, `origin`, `equip_id`, `rule`, `kind`, `cause`, `action`, `response_tags`, `start_at`, `stop_requested_at`, `end_at` |

생성기가 이미 `ceil(sim_now)+3` 보다 뒤까지 표본을 만들었으면 그 끝으로 미룬다(이미 만든 표본은 바뀌지 않는다). 응답의 `start_at`·`stop_requested_at` 이 실제 시각이다.
`POST` 는 `/ingest` 와 같은 토큰 규칙(`INGEST_TOKEN` 이 있으면 `X-Ingest-Token`, 통합 실행인데 토큰이 없으면 403)이다. 그 밖의 오류: 분리 실행 409(`embedded only`), 생성기가 죽었으면 503(`simulator stopped…`),
백필 중이거나 밀린 구간을 아직 따라잡지 못했거나 `sim_now` 를 모를 때 503(`backfilling`, `/health` 의 `live` 가 true 가 되면 가능),
모르는 설비 404, 그 설비에 주입·지속 고장이 살아 있어 주입이 겹치면 409, 규칙이 1~8 이 아니면 422. 같은 설비에 자동 이상이 진행 중이면 주입이 그 사건을 끝내고 대신한다.
정답(원인·분류·조치)은 `/alarms` 에 싣지 않는다. 심각도도 없다. 시작 시각에 바로 멈춘 사건(길이 0)은 정답지에 없다.

지속 고장은 멈출 때까지 쌓이되 공장당 최대 N개(`persistent_fault_max_open`, 기본 3)다. 상한이면 그 회차는 건너뛰고 다음 회차는 간격 뒤에 다시 본다(주입 사건은 세지 않는다).
멈추면 다음 회차에 자리가 난다. 시험 사례마다 먼저 `POST /{공장}/anomalies/stop` 으로 공장을 비운 뒤 주입한다.

## 신호 생성

모든 값은 `(seed, seed_key, KST 날짜)` 로 정해진다. 같은 시드로 다시 백필하면 같은 값이 나온다.

- 정상 신호: 태그마다 고정 μ·σ(`control`)에 대해 z 를 순차로 만든다. `z = ρ·clip(z_prev, −2, 2) + √(1−ρ²)·N(0,1)`, ρ = 0.12(온도 0.25).
  뽑은 z 가 Nelson 규칙을 위반하면 최대 100번 다시 뽑아(억제 표집) 정상 구간을 거의 깨끗하게 둔다. 값 = `round(μ + σ·z, decimals)`.
  `speed` 태그는 이상에서 바뀌지 않고, RFCC 의 `driven_by` 짝은 같은 값을 낸다. 하루 주기·드리프트·공정 부하는 없다.
- 순차 생성이라 태그별 최근 14개 z 가 상태다. 백필 → 실시간은 같은 상태로 이어지고, 재시작 때는 `ts_raw` 에서 마지막 14개 값을 읽어 상태를 되살린다.
- 이상 사건은 규칙별 z 템플릿(data-v3 `signal_engine.cpp`)을 대표 태그와 연관 태그(진동·온도·전류·압력·유량…, 지연·방향·회복 시간 포함)에 덮어쓴다. 규칙 3 은 끝을 모르는 사건이라 16 표본 주기의 톱니로 만든다(주기마다 알람이 열고 닫히지 않도록 15 표본보다 짧은 간격으로 위반이 이어진다).
  끝나면 태그마다 회복 시간 동안 z 를 0 쪽으로 줄이고 정상 표집으로 돌아간다.
- 판정은 생성기(`status` 채움)와 수집 서버가 같은 `common/nelson.py` 로 한다.

## 메시지 형식 v2

```json
{
  "schema_version": "2.0",
  "source_id": "SIM-RHDS",
  "batch_id": "b-20261109-172400-SIM-RHDS-0001",
  "sent_at": "2026-11-09T17:24:00+09:00",
  "samples": [
    { "equip_id": "P-3101", "tag_id": "TI-B-P3101", "ts": "2026-11-09T17:23:50+09:00", "value": 63.4, "status": "Good" }
  ]
}
```

`status` 는 설비 상태 `Good` / `Bad` 다. 그 표본에서 Nelson 규칙이 하나라도 성립하면 `Bad`(측정 신뢰도가 아니라 판정 결과). `value` 는 필수다. v1(`quality`)은 400 으로 거절한다.
수집 규칙: 같은 `batch_id` 와 같은 `(equip_id, tag_id, ts)` 는 한 번만 저장한다.
마스터에 없는 (설비, 태그) 쌍·형식 오류·보관 기간 밖의 행은 거절하고 응답에 건수와 사유를 돌려준다. 시각 순서가 뒤바뀐 행도 받는다. v2.x 에서 추가된 필드는 무시하고 받는다.
수집 서버는 받은 묶음을 모두 다시 판정해 `/alarms` 를 만든다(받은 `status` 와 다르면 경고 로그).

## API

루트와 공장 접두어 아래의 두 층이다. 공장 접두어는 `config/plants.yaml` 의 `key`.

| 메서드 · 경로 | 용도 |
| --- | --- |
| `GET /` | 공장 목록 페이지 |
| `GET /health` | 전체 상태. 공장마다 `plant_id`, `sim_now`, `last_received_at`, `catching_up`, `live`(백필이 끝나 실시간인지) |
| `GET /{공장}/` | 디버깅 화면. 설비별 표에 태그의 값·상태·측정 시각이 SSE 로 1초마다 갱신된다 |
| `POST /{공장}/ingest` | 시뮬레이터 묶음 수신 |
| `GET /{공장}/health` | 그 공장의 상태 |
| `GET /{공장}/equipment` | 계층 순서의 설비 목록과 태그(설명·단위·주기·자릿수·한계값·`normal_ref`·`limit_basis`) |
| `GET /{공장}/current` | 모든 태그의 현재값과 `status` |
| `GET /{공장}/equipment/{equip_id}/tags/{tag_id}/current` | 태그 현재값. `status` 는 `Good` / `Bad`, 전송 주기와 태그 주기 중 긴 쪽의 3배 동안 안 들어오면 `NoData` |
| `GET /{공장}/timeseries?equip_id=&tag_id=&from=&to=&interval=1h` | 집계 시계열 (`10m` / `1h`) |
| `GET /{공장}/alarms?equip_id=&rule=&from=&to=&open=` | Nelson 알람(`nelson_alarm`). `from`/`to` 는 구간과 겹치는 알람(5분 전에 열려 아직 열린 것도 나온다), `open=true` 는 열린 것만. 심각도·원인은 없다. `cleared_at` 은 15 연속 미성립 표본 중 첫 표본 시각으로 거슬러 적으므로, `from` = 지난 조회 시각으로 부르면 닫힌 시각이 조회 구간보다 앞선 알람이 보일 수 있다 |
| `POST /{공장}/anomalies` | 설비에 이상 주입 (토큰 필요, 통합 실행 전용) |
| `POST /{공장}/anomalies/{equip_id}/stop` | 그 설비의 이상 멈춤 |
| `POST /{공장}/anomalies/stop` | 공장의 이상 전부 멈춤 |
| `GET /{공장}/anomalies?equip_id=&from=&to=` | 정답지 (원인·분류·조치·반응 태그) |
| `GET /{공장}/stream` | SSE 로 새 값 실시간 전달 |

`from`/`to` 는 ISO 8601 이다. 쿼리 문자열의 `+` 는 공백이 되므로 `Z`(UTC) 를 쓰거나 오프셋을 `%2B09:00` 으로 인코딩한다. 읽지 못하면 400.

## DB (`data/{공장}.db`)

스키마 v3. 시각 컬럼은 모두 UTC ISO 문자열이고 태그는 `(equip_id, tag_id)` 복합키다. 배치는 컴퓨터 시계가 아니라 수신한 측정 시각의 최대값(`sim_now`)을 보고
그 시각이 지난 구간을 처리하므로, 백필에서도 실시간과 같은 경로로 집계·알람이 만들어진다.

| 테이블 | 채워지는 경로 |
| --- | --- |
| `ts_raw`, `tag_current` | 수신 즉시 |
| `ts_10m`, `ts_1h` | 10분·매시 배치. 평균·최소·최대는 모든 표본으로, `status` 는 구간에 `Bad` 가 하나라도 있으면 `Bad` |
| `nelson_alarm` | 받을 때마다 판정. (태그, 규칙)의 연속 위반을 한 행으로 묶고 15 표본 연속 미성립이면 닫는다 |
| `anomaly_event` | 정답지 겸 생성기 상태 (자동 이상·지속 고장·주입) |
| `plant`, `unit_group`, `unit`, `equipment`, `equipment_type`, `equipment_alias`, `tag_std`, `equipment_tag` | 구성안에서 서버 시작 때 |
| `ingest_batch`, `job_state`, `schema_meta` | 중복 확인, 배치 진행 상태, 스키마 버전 |

- **예전 DB 파일(v1·v2)은 열 수 없다.** 수집 서버가 파일 경로와 함께 옮기거나 지우라고 알리고 멈춘다. `data/old/` 로 옮긴 뒤 백필을 다시 돌린다. 배포(k8s)에서는 DB v3 이므로 PVC 의 기존 DB 파일을 지우고 다시 띄운다.
- 보관: 10분 배치가 끝날 때마다 `raw_retention_days` 보다 오래된 `ts_raw` 행(측정 시각 기준)과 `ingest_batch` 행(받은 시각 기준)을 지운다. 집계는 남긴다.
- 용량(하루 백필 뒤 실측, 행당 약 180 B): rfcc 1.1 GB · hou 0.7 GB · cdu 0.6 GB · rhds 0.5 GB · vrds 0.5 GB. 다섯 1초 공장을 하루 보관하면 합계 약 3.5 GB 에서 머문다. SQLite 파일은 지워도 줄지 않고 최고점에서 고정된다. 
- 수집 서버는 프로세스 하나다. 통합 실행에서는 동시에 저장하는 공장을 둘로 제한하고(나머지는 기다린다) 값 계산·저장을 워커 스레드에서 돌린다. 분리 실행으로 다섯 공장이 동시에 첫 백필을 하면 SQLite 쓰기가 직렬화돼 하루 백필이 함께 끝나기까지 약 2~5분 걸렸고, 먼저 실시간에 들어간 공장의 1초 전송은 재시도로 버틴다.

## k8s 배포 (`deploy/k8s`)

네임스페이스 `u4-2`(CPU 2 · 4Gi 를 frontend·backend·agent 와 공유)에 **파드 하나**로 올린다. 수집 서버가 다섯 공장의 생성기를 같은 프로세스에서 돌린다.

| 항목 | 값 |
| --- | --- |
| 자원 | requests 200m/512Mi, limits 500m/1Gi |
| 스토리지 | PVC `plant-sim-data` 10Gi(gp3, RWO, 매니페스트에 포함). 다섯 공장 SQLite: 원시값은 하루 보관으로 약 3.5 GB 에서 고정, 집계는 1년에 약 2 GB 증가. 늘릴 수는 있지만 줄일 수는 없다 |
| 외부 | Ingress `https://u4-2.skala-us.com/plant-sim/…` (접두어 제거, SSE 용 버퍼링 off·타임아웃 3600s). 조회·화면·SSE |
| 내부 | `http://plant-sim.u4-2.svc.cluster.local:8000/{공장}/…` (접두어 없음, CORS 불필요) |
| 쓰기 보호 | 통합 실행에서 외부 `POST /ingest` 와 이상 주입·멈춤(`POST /{공장}/anomalies…`)은 기본적으로 닫혀 있다(403). 쓰려면 Secret `plant-sim` 의 `ingest-token` 을 만들고 부르는 쪽이 `X-Ingest-Token` 을 붙인다 |
| 프로브 | startupProbe 30초×60회(첫 백필 30분까지), readiness 10초, liveness 30초. 값 계산·저장은 워커 스레드에서 돌고 조회 핸들러는 스레드풀에서 돌아 백필 중에도 응답한다. 생성기 태스크가 죽으면 `/health` 가 503 이 되어 liveness 가 다시 띄운다 |

이미지 빌드·push·배포는 `Jenkinsfile` 로 자동화돼 있다: `rootless-docker` 에이전트에서 테스트(uv 이미지 안) → 빌드(`test-<빌드번호>-<커밋8자리>`) → Harbor push(`harbor-jenkins-robot`) → GitOps 저장소 `URU42/being-gitops` 의 `k8s/plant-sim/kustomization.yaml` 이미지 태그 갱신(`gitops-github-pat`).
실제 배포는 그 저장소를 바라보는 쪽이 하므로, `deploy/k8s` 의 파일 전부(`kustomization.yaml`, `plant-sim-*.yaml`)를 GitOps 저장소의 `k8s/plant-sim/` 에 복사해 둔다(렌더 결과에 `harbor.uru42.space/uru42/plant-simulator` 가 있어야 파이프라인이 통과한다). 손으로 하려면:

```bash
# 1. 이미지 (PC 에서 빌드·push)
docker build --platform linux/amd64 -t harbor.uru42.space/uru42/plant-simulator:<TAG> . && docker push harbor.uru42.space/uru42/plant-simulator:<TAG>
# 2. kustomization.yaml 의 images.newTag 를 그 태그로 바꾼다 (주소는 harbor.uru42.space/uru42 로 적혀 있다)
# 3. Secret (이상 주입·멈춤 API 나 외부 /ingest 를 쓸 때 필수): kubectl -n u4-2 create secret generic plant-sim --from-literal=ingest-token='<긴 문자열>'
# 4. 적용·확인
kubectl apply -k deploy/k8s -n u4-2
kubectl -n u4-2 rollout status deploy/plant-sim
curl https://u4-2.skala-us.com/plant-sim/health
```

- 이미지는 Harbor 비공개 프로젝트에 있으므로 Deployment 에 `imagePullSecrets: harbor-cred` 를 적어 둔다(네임스페이스에 그 Secret 이 있어야 한다: `kubectl -n u4-2 get secret harbor-cred`).
- **DB v3 — 이전 버전에서 올릴 때는 PVC 의 기존 DB 파일(`/app/data/*.db*`)을 지우고 다시 띄운다.** 이전 파일은 열리지 않는다(수집 서버가 알리고 멈춘다).
- 지속 고장 간격은 Deployment 환경 변수 `PERSISTENT_FAULT_INTERVAL_MIN`(0 이면 끔)으로 조절한다.
- 첫 기동은 다섯 공장 하루치 백필이다. CPU 500m 에서 수 분~십수 분 걸리며 그동안 `/health` 의 `sim_now` 가 전진한다.
- 백필 기간을 늘리려면(`--backfill-days`) PVC 의 DB 파일을 지우고 다시 채워야 한다. 재개는 마지막 시각부터만 이어 간다.
- 다른 파드는 DB 파일을 직접 읽을 수 없다(RWO). HTTP API(`/current`, `/timeseries`, `/alarms`, `/anomalies`, `/stream`)를 쓴다.
- 실측 (다섯 공장 통합 실행, 하루 백필):

  | 조건 | 백필 중 `/health` 응답 | 하루 백필 | 메모리 |
  | --- | --- | --- | --- |
  | 로컬 Mac, 제한 없음 | 0.1초 이하 | 약 7분 | Python RSS 약 250 MB |
  | `docker run --cpus 0.5 --memory 1g` (배포 한도) | 0.1~3.7초, 대개 2~3초 (CPU 쿼터를 백필과 나눠 쓴다) | 약 15분 | cgroup 기준 785~812 MiB (SQLite 파일 캐시 포함, 회수 가능) |

  그래서 readiness·liveness 의 timeout 은 10초, startupProbe 는 30분이다. 백필이 끝나면 응답은 수 ms 로 돌아온다. 더 빨리 띄우려면 limits cpu 를 1 로 올린다.

## 폴더

```text
config/      plants.yaml · plant_*.yaml · runtime_*.yaml
common/      config_models(복합키·TagRef·한계값·control) · message(v2) · nelson(8종 판정) · sim_clock · timefmt
simulator/   nelson_model(정상 신호 + 사건 덧씌우기) · anomaly(모양·반응 태그·분류) · schedule(자동 이상 일정) · sender · main
collector/   app(라우트) · plants(공장 목록) · plant_context(공장별 문맥·수신 핵심) · embedded(통합 실행) · settings · ingest · alarms(Nelson 판정·nelson_alarm) · anomaly_book(사건 장부) · storage · schema.sql · jobs · seed_master · stream · static/index.html
deploy/k8s/  kustomization · plant-sim-{deployment,service,pvc,ingress}.yaml · plant-sim-secret.example.yaml (being-gitops k8s/plant-sim 으로 복사)
docs/        superpowers/specs (RFCC · 다중 공장 설계) · superpowers/plans (구현 계획)
tests/       기획서 9.2 단계별 완료 기준 + 설계 §5 테스트
```
