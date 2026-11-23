# plant-simulator

Skadi 앞단 데이터 생성기. 가상 공장 다섯 곳의 설비 센서 값을 끊임없이 만들어 중앙 수집 서버로 보내고,
수집 서버가 공장마다 SQLite 파일 하나에 저장·집계한다. 정상 운전은 조용하고(Nelson 규칙 1~3 위반 없음), **가상 시나리오(고장 모드)를 주입했을 때만**
그 설비와 축으로 이어진 설비의 센서 값이 움직인다. 이상 판정·알람은 하지 않는다(탐지 파이프라인 몫). Skadi 화면·backend·AI Agent 는 HTTP API 로 읽는다.

| 키(경로) | plant_id | 공장 | 설비 · 태그 | 주기 | 근거 |
| --- | --- | --- | --- | --- | --- |
| `/rfcc/` | PL-RFCC | RFCC(중질유 유동상 촉매 분해) | 12 · 82 (합성 1) | 온도 10초 · 나머지 1초 | [RFCC 설계](docs/superpowers/specs/2026-11-09-rfcc-simulator-design.md), 샘플 `rfcc-20261109/` |
| `/cdu/` | PL-CDU | 상압증류 | 7 · 46 | 온도 10초 · 나머지 1초 | [다중 공장 설계](docs/superpowers/specs/2026-11-09-multi-plant-design.md), 샘플 `cdu-sample/` |
| `/hou/` | PL-HOU | 수첨분해 | 7 · 62 (합성 1) | 온도 10초 · 나머지 1초 | 같은 설계, 샘플 `hou-plant-sample/` |
| `/rhds/` | PL-RHDS | 상압 잔사유 수첨탈황 | 9 · 58 (합성 1) | 온도 10초 · 나머지 1초 | 같은 설계, 샘플 `RHDS_VRDS_v3/` |
| `/vrds/` | PL-VRDS | 감압 잔사유 수첨탈황 | 7 · 48 (합성 1) | 온도 10초 · 나머지 1초 | 같은 설계, 샘플 `RHDS_VRDS_v3/` |

샘플 묶음 폴더는 git 밖이다. 거기서 옮긴 마스터 값(계층, 설비, 태그명, 설명, 단위, 정상 기준, 경고/알람/정지 상·하한(참고값), 근거)과 Nelson 기준 μ·σ(`baselines.json`)가 `config/plant_*.yaml` 에 있다. 설계와 계획은 [기획서](plant-simulator-plan.md), [시나리오 주입형 전환 계획](docs/superpowers/plans/2026-11-19-scenario-driven.md). 2026-11-18 까지의 자동 이상·Nelson 알람 설계는 [Nelson 이상 설계](docs/superpowers/specs/2026-11-12-nelson-anomaly-design.md)(대체됨).

```text
[config/plants.yaml → plant_X.yaml · runtime_X.yaml] × 5
        │
        ▼
[수집 서버 (FastAPI) ──┬── 통합 실행: 공장별 생성기가 같은 프로세스에서 LocalSender 로 넣는다 (--with-simulators, k8s·compose 기본)
                       └── 분리 실행: 시뮬레이터 프로세스가 HTTP POST /{공장}/ingest 로 보낸다 (compose --profile split)]
        │  공장마다 저장 · 집계 · 보관 삭제 · 고장 모드 사건 장부(정답지)
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
| 집계 배치 | 집계·보관은 수집 서버 안에서 측정 시각 기준으로 실행(별도 스케줄러 없음) |
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
# 백필: 지난 하루를 1시간 묶음으로 보낸 뒤 실시간으로 이어진다.
# 재시작하면 수집 서버의 마지막 측정 시각부터 이어서 보내므로 다시 채우지 않는다.
uv run python -m simulator.main --plant config/plant_cdu.yaml --runtime config/runtime_cdu.yaml \
    --time-mode backfill

# 실시간만: 1초마다 값 생성·전송 (온도 태그는 10초마다)
uv run python -m simulator.main --plant config/plant_cdu.yaml --runtime config/runtime_cdu.yaml
```

`runtime_X.yaml` 의 `collector_url` 이 `http://localhost:8000/cdu` 처럼 공장 접두어까지 가리킨다.
분리 실행은 정상 운전만 나온다. 고장 모드 주입·멈춤·정답지(`/anomalies`)는 통합 실행에서만 쓴다.

브라우저: `http://localhost:8000/` 은 공장 목록, `http://localhost:8000/cdu/` 는 그 공장의 현재값이 1초마다 갱신되는 디버깅 화면(값·상태·측정 시각만).

Docker Compose 기본은 통합 실행 컨테이너 하나(`plant-sim`). 분리 실행(수집 서버 1 + 시뮬레이터 5)은 `split` 프로필이다. DB 는 네임드 볼륨 `plant-data` 에 둔다.
macOS Docker Desktop 의 바인드 마운트(`./data`) 위에서는 SQLite WAL 파일이 손상된 적이 있어 쓰지 않는다. 파일이 필요하면 `docker compose cp plant-sim:/app/data/cdu.db ./data/` 로 꺼낸다.

```bash
docker compose up --build -d                    # 통합 실행 1 컨테이너
docker compose --profile split up --build -d    # 분리 실행 6 컨테이너
INGEST_TOKEN=<긴 문자열> docker compose up -d   # 고장 모드 주입·멈춤을 쓰려면 토큰
```

**DB v5 로 올릴 때(이전 버전의 볼륨이 있으면)**: 이전 DB 파일(v4 까지: Nelson 알람·규칙 번호 사건·status 칸)은 열리지 않으므로 볼륨을 지우고 다시 띄운다(재백필). `docker compose down -v` (또는 `docker volume rm <프로젝트>_plant-data`).

확인:

```bash
curl localhost:8000/health                                   # 다섯 공장의 sim_now · catching_up · live
curl localhost:8000/rhds/equipment                           # 계층 순서의 설비·태그·한계값(참고)·운전점 μ/σ
curl localhost:8000/rhds/current
curl localhost:8000/rhds/equipment/P-3101/tags/TI-B-P3101/current
curl "localhost:8000/cdu/timeseries?equip_id=P-8101A&tag_id=PI-8101A&from=2026-11-08T00:00:00%2B09:00&to=2026-11-09T00:00:00%2B09:00&interval=10m"
curl localhost:8000/rhds/fault-modes                         # 걸 수 있는 고장 모드와 대상 설비
curl localhost:8000/rhds/anomalies                           # 정답지(고장 모드·분류·단계·태그별 효과 시작)
curl -N localhost:8000/rfcc/stream
```

### 탐지 시험 흐름

토큰을 주고 통합 실행으로 띄운 뒤, 고장 모드를 넣고 → 탐지 쪽이 `/timeseries`·`/stream`·`/current` 로 값을 읽어 판정하고 → 정답지와 대조한다.

```bash
INGEST_TOKEN=t uv run python -m collector.app --plants config/plants.yaml --with-simulators --time-mode backfill
H='X-Ingest-Token: t'

curl localhost:8000/rhds/health                               # "live": true 가 될 때까지 기다린다 (그 전엔 POST 가 503)
curl localhost:8000/rhds/fault-modes                          # CC-SURGE → C-3101, OP-LOAD-CHANGE → M-3101·M-3102 …
curl -XPOST -H "$H" -H 'content-type: application/json' \
     localhost:8000/rhds/anomalies -d '{"equip_id":"C-3101","mode":"CC-SURGE"}'   # → event_id, start_at
curl -XPOST -H "$H" localhost:8000/rhds/anomalies/C-3101/stop # 그 설비만 정상으로 (60초 회복, 손상 단계를 지났으면 손상은 남는다)
curl localhost:8000/rhds/anomalies/damaged                    # 멈췄지만 손상이 남은 사건
curl -XPOST -H "$H" localhost:8000/rhds/anomalies/GB-3101/repair # 남은 손상 수리 (60초 회복)
curl "localhost:8000/rhds/anomalies?from=2026-11-19T00:00:00Z" # 정답지 → 채점에만 쓰고 탐지·요약 입력에는 주지 않는다
```

정상 운전에는 이상이 없으므로 주입한 것 = 정답이다.

## 명령행 인자

| 프로세스 | 인자 | 환경 변수 | 기본값 |
| --- | --- | --- | --- |
| 수집 서버 | `--plants` | `PLANTS_CONFIG` | `config/plants.yaml` |
| 수집 서버 | `--host`, `--port` | `HOST`, `PORT` | `0.0.0.0`, `8000` |
| 수집 서버 | `--with-simulators` | `WITH_SIMULATORS` | 꺼짐. 켜면 공장 목록의 생성기를 같은 프로세스에서 돌린다 |
| 수집 서버 | | `REDIS_URL` | 있으면 받은 값을 Redis Stream `plant-sim:data:{공장}` 으로 보낸다(아래). k8s 는 `redis://clx-redis:6379` |
| 수집 서버 | `--time-mode`, `--backfill-days` | | 통합 실행에서 각 공장 runtime 값을 덮어쓴다 |
| 수집 서버·시뮬레이터 | `--profile` | `DATA_PROFILE` | `demo`. 실행 용도 — `demo` 는 runtime 파일 그대로(시연용 조용한 정상), `eval` 은 학습·평가용(`quiet_normal: false`, 계측 이상 켬). DB 마다 하나로 고정된다(아래) |
| 수집 서버 | | `ROOT_PATH` | 경로 접두어(예: `/plant-sim`). 루트 링크·리다이렉트·`/docs` 가 붙인다 |
| 수집 서버 | | `CORS_ORIGINS` | 쉼표 구분. 있으면 GET 에 CORS 헤더 |
| 수집 서버 | | `INGEST_TOKEN` | 있으면 외부 `POST /ingest` 와 `POST /anomalies…`(주입·멈춤)는 헤더 `X-Ingest-Token` 필수. 통합 실행인데 없으면 둘 다 닫힌다(403). 통합 실행의 생성기는 거치지 않는다. 시뮬레이터 `HttpSender` 도 같은 변수를 읽어 헤더를 붙인다 |
| 시뮬레이터 | `--plant` | `PLANT_CONFIG` | `config/plant_rhds.yaml` |
| 시뮬레이터 | `--runtime` | `RUNTIME_CONFIG` | `config/runtime_rhds.yaml` |
| 시뮬레이터 | `--time-mode` | | `runtime.yaml` 의 `time_mode` |
| 시뮬레이터 | `--collector-url` | `COLLECTOR_URL` | `runtime.yaml` 의 `collector_url` |

- Unit 단위로 나눠 돌리려면 runtime 파일을 복사해 `units: [UNIT-CDU-01]`, `source_id: SIM-CDU-1` 처럼 바꾸고 프로세스를 여러 개 띄운다. 값은 한 프로세스로 돌릴 때와 같다.

## 설정 파일

| 파일 | 내용 |
| --- | --- |
| `config/plants.yaml` | 수집 서버가 여는 공장 목록: `key`(URL 접두어), 구성안·실행 파일, DB 파일. `fault_modes`(기본 `config/fault_modes.yaml`). `plants.k8s.yaml` 은 DB 경로가 `/app/data/` 인 k8s 용 |
| `config/plant_{rfcc,cdu,hou,rhds,vrds}.yaml` | 구성안: 계층, 설비 유형(`kind`), 표준 태그, 공통 요인(`correlation`), 설비(`operating` 운전점), 태그(설명·단위·주기·자릿수·한계값(참고)·`normal_ref`·`limit_basis`·`seed_key`·`control` 의 설계점 μ·σ) |
| `config/fault_modes.yaml` | 고장 모드(가상 시나리오): 단계별로 어떤 신호가 어떤 모양·크기·지연으로 움직이는지, 정답 분류(`label`), 평가용 기대값(`expect`). 다섯 공장이 함께 쓴다 |
| `config/runtime*.yaml` | 실행 설정: Unit 목록, `source_id`, 주기, 시간 모드, 가속 배율, 백필 기간, 시드, 수집 서버 주소(공장 접두어 포함), 원시 값 보관 기간 |

### 태그를 가리키는 법

태그는 **(설비 ID, 태그명)** 복합키다. 다섯 공장의 태그명은 공장 안에서 고유하다(RHDS·VRDS 는 자료에서 겹치던 `TI-B-3101` 등에 설비 종류 문자를 넣어 `TI-B-P3101`·`TI-B-C3101`·`TI-B-F3101` 로 바꿨다).
API 는 늘 두 값을 함께 받는다(`equip_id=P-3101&tag_id=TI-B-P3101`, `/equipment/P-3101/tags/TI-B-P3101/current`). 설비끼리의 관계는 `driven_by`(구동기 설비 ID) 와 같은 공정 그룹(공통 요인)뿐이다.

### 한계값과 관리 기준

```yaml
limits: { warn_hi: 3.0, alarm_hi: 4.5, trip_hi: 7.1 }     # 자료의 참고값(상한만)
control: { mean: 2.31, sigma: 0.08 }                       # 설계 운전점의 관리 기준 μ·σ — 생성과 탐지 쪽 Nelson 기준
```

`limits` 여섯 칸은 자료에 있는 것만 적는 참고값으로 마스터(`/equipment`)에 남긴다. 고장 모드가 `to_limit` 으로 크기를 정할 때 쓰고, 판정은 하지 않는다.
RFCC 는 자료에 통계 경고(μ ± 3σ)만 있어 2026-11-19 에 알람 μ ± 4.5σ·정지 μ ± 6σ 를 합성으로 더했으나(D23), 2026-11-23 공학 기준값으로 바꿨다(사용자 확정, synthetic).
다른 공장 같은 신호의 규칙을 옮겼다(펌프 진동 3.0/4.5/7.1 mm/s·베어링 온도 절대값, 유량·압력·전류·회전수 등은 μ 대비 비율 중앙값, 축 진동은 ISO 7919-3 회전수 식,
증기는 설계값 ±10/15/20 %, 대기압 가까운 압력 2개는 따로). 근거는 태그별 `limit_basis`. μ·σ(Nelson 관리 기준)는 그대로다.
그래서 다른 공장처럼 한계가 정상에서 수십 σ 떨어져 있고, 정상 운전 변화(−1.5σ)는 경고에 닿지 않는다. `normal_ref`·`limit_basis` 는 자료 원문 참고 문자열이다.

### 공정 흐름 그래프 (`flow`)

설비 간 고장 전파에 쓰는 프로젝트 가상 공장의 흐름 그래프(2026-11-20). 3D 뷰어 그래프(2026-11-19)에서 옮겼고, 이제 이 구성안이 정본이며 뷰어는 `GET /{공장}/flow` 로 읽는다.

```yaml
flow:
  nodes: { MX: "원료·수소 혼합기", HX: "원료·반응 유출물 열교환기", ... }   # 설비가 아닌 공정 노드
  edges:                                      # [from, to, label, stream]
    - [P-3101, MX, "", process]
    - [MX, HX, "저온측", process]              # 열교환기는 들어온 쪽과 같은 이름의 출구로만 나간다
    - [C-3102, C-3101, 보충 수소, utility]      # 가상 공장 추가 (뷰어 원래 그래프에 없음)
```

- `stream`: `process`(원료·탄화수소 흐름) 또는 `utility`(수소·연소공기·윤활유·세정수 등 보조 계통). 뷰어의 계통 구분 규칙을 옮긴 값이다.
- 축 연결은 `driven_by` 가 맡아 여기에 넣지 않는다. 노드 ID 는 설비 ID 와 겹치면 안 되고, 간선 끝은 설비나 공정 노드여야 한다.

### 운전점 (`operating`)

```yaml
- equip_id: T-1401
  operating: { speed_rpm: 8400 }      # 시작 운전점. 설계 rpm 은 그 설비 speed 태그의 control.mean
```

구동기(`driven_by` 없음)이고 speed 태그가 있는 설비에만 둔다. 설계 대비 비 r 로 그 설비와 축으로 이어진 아래쪽 설비의 태그 μ 를 바꾼다:
회전속도·유량 r, 진동 r², 차압 r², 전류 r³, 토출 압력은 같은 설비 흡입 압력과의 차(양정)에 r² (상사 법칙 근사, synthetic). σ·온도·한계값은 그대로다.
바뀐 μ 는 마스터(`/equipment` 의 `control`, `equipment_tag.control_mean`)에 그대로 나가므로 탐지 쪽 Nelson 기준도 같이 옮겨 간다.
실행 중에는 바꾸지 않는다 — 운전 중 부하 변화는 고장 모드 `OP-LOAD-CHANGE`(정답: 정상 운전 변화)로 넣는다.
팬 오염 × 회전수 비교(2026-11-23)도 이 방식이다: 같은 팬에 운전점을 다르게 준 실행(예: 설계 ×0.8, ×1.05)마다 `FN-IMPELLER-FOULING` 을 걸어 비교한다.
운전점은 그 팬 회전수 태그의 경고·경보 한계 안에서 고른다(예: RHDS F-3101 설계 1,464 rpm, 경고 하한 1,150·상한 1,550 → ×0.8~×1.05).

### 고장 모드 (가상 시나리오)

정상 운전에는 자동 이상·지속 고장이 없다. 이상은 `POST /{공장}/anomalies {"equip_id","mode"}` 로 고장 모드를 주입할 때만 생긴다.
`config/fault_modes.yaml` 의 고장 모드는 단계(`stages`)마다 효과(`effects`)를 적는다.

```yaml
- id: ST-WATER-INDUCTION
  equip_types: [ST]                  # 또는 equip_ids: [C-1401, …]
  label: { kind: 기계 이상, cause: …, action: … }      # 정답지에만
  stages:
    - { id: S0, at_sec: 0, nelson_quiet: true, effects: [ { signal: shaft_vibration, shape: ramp, z: 0.8, rise_sec: 300 } ] }
    - id: S3
      at_sec: 1020
      effects: [ { signal: shaft_vibration, shape: step, to_limit: warn_hi, margin: 0.2 } ]
      propagate: { driven: [ { signal: flow, shape: ramp, z: -6, lag_sec: 30, rise_sec: 600 } ] }
  expect: { … }                      # 평가용 기대값 (자유 형식, 정답지에만)
```

| 항목 | 뜻 |
| --- | --- |
| `signal` | 태그가 아니라 신호 종류로 지정한다. 그 설비의 같은 신호 태그가 모두 움직인다 |
| `shape` | `step`(바로) · `ramp`(`rise_sec` 동안 0→z 뒤 유지) · `spike`(`rise_sec` 동안 0→z, `decay_sec` 동안 z→0) · `wobble`(진폭 z, 주기 `period_sec`) |
| `z` | 크기(관리 기준 σ 배수). 앞 효과에 더한다 |
| `delta` | 크기(태그 단위, 예: +15 ℃). z = delta/σ 로 바꿔 쓴다. σ 가 설비마다 달라도 같은 물리량만큼 움직인다 |
| `to_limit`, `margin` | 그 태그의 효과 합을 한계값(`warn_hi`·`alarm_lo` …)까지, (한계 − μ)의 `margin` 만큼 더. 앞 단계 step·ramp 가 남긴 수준을 빼고 그만큼만 움직인다. 태그에 그 한계가 없으면 `z`, 둘 다 없으면 그 태그는 건너뛴다 |
| `lag_sec` | 단계 시작 뒤 지연 |
| `boost` (모드) | 교호작용 `{by, signals, per_unit 또는 exponent, max_gain}`. 선형(`per_unit`): 주입 설비의 `by` 신호 태그가 사건으로 오른 만큼(ΔT, 태그 단위, 정상 잡음 제외) `signals` 효과를 배율 `min(max_gain, 1 + per_unit × ΔT)` 로 키운다. 비율 거듭제곱(`exponent`): 배율 `min(max_gain, (N / N_설계)^exponent)`, N = 운전점 μ + 사건 효과(낮으면 1 보다 작다). 이때 키우는 태그의 효과 크기(`to_limit` 포함)는 설계 운전점 μ 로 정한다(운전점 μ 로 정하면 한계 목표가 회전수 차이를 상쇄한다). 하류·축 전파 효과는 키우지 않는다. `hold_below_alarm: true` 면 그 태그의 경보 한계 목표 효과가 시작되기 전에는 키운 효과가 경보 한계 3σ 안쪽에 머물고(경고 구간 보장), 그 효과의 상승 시간 동안 서서히 풀린다 |
| `propagate.driven` | 축으로 이어진 아래쪽 설비(`driven_by` 사슬) 전부에 주는 효과. 피구동 설비 `speed` 는 늘 구동기 값을 그대로 쓴다 |
| `propagate.process` | 공정 흐름 그래프(`flow`)로 닿은 하류 설비에 주는 효과(1단계 기준). `z` 만 쓰고(`to_limit` 불가) 모양은 `ramp`·`wobble` |
| `propagate.utility` | 보조 계통 간선으로 바로 닿은 설비(윤활유 펌프 → 압축기, 보충 수소 → 순환 압축기)에 주는 효과. 그 설비에서 멈춘다 |
| `damage` | 손상 단계. 그 단계의 step·ramp 효과는 멈춘 뒤에도 멈춘 시각의 크기로 남고, 수리(`POST …/repair`) 뒤 60초(하류 k단계는 × (k+1)) 동안 0 이 된다. spike·wobble 은 남지 않는다. 대상 단계는 이름(손상·마모·오염·침식·누설·파손)을 따라 골랐고 사용자 확정(2026-11-23)이다. 수리는 API·화면으로만 하며 백엔드 조치 완료와는 연결하지 않는다(사용자 결정 2026-11-23, 바뀔 수 있다) |
| `nelson_quiet` | 그 단계 동안은 효과를 더한 값도 Nelson 규칙 1~3 을 어기지 않게 잡음을 고른다. Setpoint·Nelson 은 못 보고 ML 만 볼 수 있는 전조를 만드는 **합성 장치** |

- 같은 설비에는 안 멈춘 사건이 하나뿐이다(겹치면 409). 사건은 멈출 때까지 가고, 멈추면 효과가 60초(하류 k단계는 60초 × (k+1)) 동안 0 으로 가라앉는다.
  단, 멈추기 전에 시작한 손상 단계(`damage`)의 step·ramp 효과는 수리할 때까지 남는다(멈춤 = 원인 해제, 수리 = 손상 회복). 손상이 남은 설비에도 새 사건을 걸 수 있고 효과는 더해진다.
- 공정 흐름 하류 전파 (2026-11-20, 사용자 확정, 수치는 synthetic): 고장 설비와 그 축 사슬을 0단계로, 흐름 그래프를 따라 설비에 닿을 때마다 1단계씩
  최대 2단계까지 퍼진다. **완만한 감쇠**: k단계 크기 = 1단계 × 0.7^(k−1), `lag`·`rise` × k(늦고 천천히 오른다), 회복 60초 × (k+1).
  줄인 크기가 0.25σ 이하면 걸지 않는다. utility 간선은 출발 설비와 utility 로 닿은 공정 노드에서만 지나고(공정 흐름에 들어선 뒤 보조 계통으로
  퍼지지 않는다), utility 로 닿은 설비에서는 멈춘다(정지(trip)는 모의하지 않는다). 전파하는 모드: `PM-SUCTION-LOSS`·`CC-SURGE`·
  `FN-IMPELLER-FOULING`·`OP-LOAD-CHANGE`·`RC-VALVE-LEAK`. 근거·도달 표는 위키 raw 초안 `PSIM-LINK-20261120` v0.3.
- 효과는 단계가 끝나도 남는다(예: S0 의 wobble 은 S2·S3 에도 계속된다). `nelson_quiet` 는 그 단계 동안만이다.
- 정답지의 `end_at` 은 멈춤 + 60초(멈추기 전에 시작한 하류 효과가 있으면 가장 먼 단계 k 의 60초 × (k+1))다. 손상이 남은 사건(`damage_remains`)은 수리 + 회복이고, 안 고쳤으면 null 이다. 그 뒤 규칙 창(8표본, 10초 태그는 80초) 안의 위반은 사건 꼬리다.
- 시나리오는 **지금 있는 설비·태그만으로** 표현한다. 고장 모드가 가리킨 신호의 태그가 그 설비에 없으면 건너뛴다. 실제 고장이면 보일 신호지만 태그가 없어 만들지 않는 것은 모드의 `unobserved` 에 적고 `/fault-modes`·정답지에 싣는다(예: 드레인 밸브 상태, 밸브 커버 온도). 태그를 더할지는 따로 정한다.
- 계산 태그 (구성안 `derive`, 2026-11-19, synthetic): 같은 설비 안 관계를 매 표본 지킨다. 값 = 그 시각 입력 태그 값의 계산식
  + 자체 잡음(`noise_sigma`) + 고장 효과. 입력이 움직이면 따라 움직이고(정답지에도 입력 효과의 단계·시작으로 실린다), 자기 효과는 관계를 깬다.
  - 자료 태그 40개: 펌프 전류 ∝ 유량 × 양정(토출 − 흡입), 팬 전류 ∝ 유량³·차압 ∝ 유량², 압축기 토출 온도(K) ∝ 흡입 온도(K) × 압축비^m
    (m 은 자료 μ 로 맞춤), 베어링 온도(K) ∝ 윤활유 온도(K). 물리식 우선(사용자 확정): μ 는 자료 그대로, σ 는 식이 정하는 변동이 자료 σ 를
    넘으면 정상 생성값으로 바꿨다(35개, 자료의 1.1~5.2배 — 공통 요인이 유량·토출 압력을 같은 쪽으로 움직여 곱이 커진다). RFCC 의 μ±3σ 경고 한계는
    새 σ 로 다시 계산했고(2026-11-23 공학 기준값으로 바뀜), 다른 공장의 자료 한계값은 새 σ 로도 6σ 이상 떨어져 그대로 두었다. 운전점의 μ 이동도 식을 따른다(팬은 상사 법칙과 같다).
  - 합성 태그 4개(자료에 없음, description 에 `(synthetic)`): `STT-T-1401` 입구 증기 온도 = `SP-T-1401` 의 포화 온도(IAPWS-IF97) + 과열도 150 ℃ 가정,
    `PI-2201C`·`PI-I-C3102`·`PI-I-C4102` 단간 압력 = √(흡입·토출 절대압) (2단·단별 압력비 같음 가정, 한계값 없음). σ 는 정상 생성값에서 잰 값.
  - 억제 표집은 자체 잡음만 다시 뽑으므로 계산 태그는 정상에서도 규칙 위반(하루 약 0.26 %)과 한계 넘음(0.004 %, RFCC 펌프 전류)이 가끔 난다.
    실제 공장처럼 정상에도 넘는 일이 있다는 것을 받아들인 것이며, 정상 여부는 정답지로 가린다. 나머지 태그의 규칙 위반은 0 이다.
- 지금 들어 있는 모드 11개 — 다섯 공장 42대 모두 하나 이상 걸 수 있다(테스트로 지킨다). 수치·지연은 모두 synthetic 이며 공정·설비 검토 전이다.

  | 모드 | 대상 | 시나리오 |
  | --- | --- | --- |
  | `ST-WATER-INDUCTION` | 증기터빈 (`T-1401`) | 수분 유입 → 열충격·액체 충돌 → 로터·블레이드 손상. 피구동 `C-1401` 로 전파 |
  | `CC-SURGE` | 원심 압축기 4대 + RFCC 주 공기 송풍기 `B-1201` | 저유량 → 서지(압력·유량 진동) → 스러스트 베어링 충격 |
  | `RC-VALVE-LEAK` | 왕복동 압축기 `C-2201`·`C-3102`·`C-4102` | 밸브 누설 → 재압축 손실 → 토출 온도 상승·유량 저하 |
  | `RC-LIQUID-INGRESS` | 왕복동 압축기 3대 | 액체 유입 → 액체 충격 → 밸브·피스톤·로드 손상 후보 → 파손(S4, 25분부터 정지 한계) |
  | `PM-SUCTION-LOSS` | 펌프 24대 | 흡입 압력 저하 → 진동 증가·유량 저하 → 이상 심화 → 심한 캐비테이션(S4, 30분부터 정지 한계) |
  | `PM-SUCTION-HOT` | 흡입 유체 온도 태그가 있는 펌프 `P-3102`·`P-4102`·`P-8303A` | 위와 같은 단계에 S1 유체 온도 +15 ℃. 교호작용(`boost`)으로 진동·유량 효과 ×1.45(계수 0.03/℃, 상한 ×2.0, 경고 구간 보장). 경고 단계는 남고 경보·정지가 이르다 |
  | `FN-IMPELLER-FOULING` | 팬 4대 | 임펠러 오염 → 불균형 진동 → 베어링 온도 상승. 진동·베어링 온도는 회전수 교호작용(`boost`, 지수 2): 운전점 ×0.8 이면 ×0.64, ×1.05 면 ×1.10 |
  | `EX-CATALYST-EROSION` | 팽창 터빈 `E-1202` | 촉매 미립자 부착·침식 → 회수 동력 감소(피구동 `B-1201` 유량) → 블레이드 손상 위험 |
  | `GB-LUBE-WEAR` | 기어박스 `GB-3101` | 윤활 불량 → 치면 마모. 피구동 `C-3101` 진동으로 전파 (기어박스는 한계값이 없다) |
  | `OP-LOAD-CHANGE` | 구동기 `T-1401`·`M-3101`·`M-3102`·`M-4102`, HOU 압축기 `C-2201`·`C-2301` | 운전 부하 감소(회전속도·전류·유량·토출 압력 하향) — 정답은 "정상 운전 변화". 팬은 제외 |
  | `OP-THROUGHPUT-CHANGE` | 원료 공급 펌프 `P-8101A`(CDU)·`P-1101A`(RFCC)·`P-2101`(HOU)·`P-3101`(RHDS)·`P-4101`(VRDS) | 처리량 감소(공급 유량·토출 압력 하향, 하류 펌프 처리량도 감소) — 정답은 "정상 운전 변화" |

### 이상 API (통합 실행 전용)

| 요청 | 동작 |
| --- | --- |
| `GET /{공장}/fault-modes` | 이 공장에서 걸 수 있는 고장 모드와 대상 설비·단계·`unobserved`. 정답 분류는 싣지 않는다 |
| `POST /{공장}/anomalies` `{"equip_id","mode","speed"}` | 시뮬레이터 시각 기준 `ceil(sim_now)+3`초 첫 표본부터 사건 시작. `speed` 는 진행 속도 1·3·5·10 (기본 1). 201 + `{event_id, equip_id, mode, speed, start_at}` |
| `POST /{공장}/anomalies/{equip_id}/stop` | 그 설비의 안 멈춘 사건을 `ceil(sim_now)+3`초에 멈추고 회복. 200 + `{"stopped": [event_id…], "stop_requested_at"}`. 사건이 없으면 404 |
| `POST /{공장}/anomalies/stop` | 공장의 안 멈춘 사건 전부를 같은 방식으로 멈춘다. 200 + `{"stopped": [event_id…], "stop_requested_at"}`(없으면 빈 목록) |
| `POST /{공장}/anomalies/{equip_id}/repair` | 그 설비의 멈췄지만 손상이 남은 사건을 `ceil(sim_now)+3`초에 고친다(손상 효과 회복). 200 + `{"repaired": [event_id…], "repair_requested_at"}`. 고칠 손상이 없으면(안 멈춘 사건 포함) 404 |
| `GET /{공장}/anomalies/damaged` | 멈췄지만 손상이 남아 안 고친 사건 `[{event_id, equip_id, mode, start_at, stop_requested_at}]` (화면의 수리 버튼용) |
| `GET /{공장}/anomalies/active` | 안 멈춘 고장 모드 사건 `[{event_id, equip_id, mode, start_at}]`. 정답 분류는 싣지 않는다 (화면의 적용/해제 상태용) |
| `GET /{공장}/anomalies?equip_id=&from=&to=` | 정답지: `event_id`, `equip_id`, `mode`, `code`, `kind`, `cause`, `action`, `expect`, `stages`(단계별 시작), `response_tags`(태그·단계별 효과 시작, `via`: self·driven·process·utility, `hop`: 흐름 단계 — 2026-11-20 전 사건은 null), `unobserved`, `start_at`, `stop_requested_at`, `damage_remains`(멈춘 뒤 손상이 남았는가), `repair_requested_at`, `end_at`. `response_tags` 의 `damage` 는 수리 전까지 남는 효과. `boost` 는 교호작용 규칙(없으면 null), `response_tags[].boost_by` 는 교호작용으로 키운 효과의 by 태그. 고장 모드와 계측 이상(켰을 때)을 시작 순으로 함께 싣는다 |

생성기가 이미 `ceil(sim_now)+3` 보다 뒤까지 표본을 만들었으면 그 끝으로 미룬다(이미 만든 표본은 바뀌지 않는다). 응답의 `start_at`·`stop_requested_at` 이 실제 시각이다.
`POST` 는 `/ingest` 와 같은 토큰 규칙(`INGEST_TOKEN` 이 있으면 `X-Ingest-Token`, 통합 실행인데 토큰이 없으면 403)이다. 그 밖의 오류: 분리 실행 409(`embedded only`), 생성기가 죽었으면 503(`simulator stopped…`),
백필 중이거나 밀린 구간을 아직 따라잡지 못했거나 `sim_now` 를 모를 때 503(`backfilling`, `/health` 의 `live` 가 true 가 되면 가능),
모르는 설비 404, 그 설비에 안 멈춘 사건이 있으면 409, 모르는 고장 모드이거나 그 설비에 걸 수 없으면 422.
정답지는 주입할 때 적은 계획을 그대로 쓰므로 나중에 `fault_modes.yaml` 을 고쳐도 바뀌지 않는다. 멈추기 전에 시작하지 못한 단계·효과와 길이 0 인 사건은 싣지 않는다.

정답지 코드 `code` (사용자 확정 2026-11-19): **1 정상**, **0 비정상(설비 고장)**, **2 계측 이상(설비 고장 아님)**. 사건이 없는 구간은 1 이다.
고장 모드는 `label.code`(기본 0, `OP-LOAD-CHANGE` 는 1), 계측 이상은 2.

### Redis Stream (측정값) 과 브라우저 로그

수집 서버는 받은 값을 공장별로 **초마다 메시지 하나**로 묶어 Redis Stream 에 보낸다 (사용자 확정 2026-11-19, `REDIS_URL` 이 있을 때).

| 항목 | 값 |
| --- | --- |
| 스트림 | `plant-sim:data:{공장 키}` — `plant-sim:data:rfcc`, `…:cdu`, `…:hou`, `…:rhds`, `…:vrds` |
| 엔트리 필드 | `data` 하나. 값은 JSON 문자열 |
| `data` | `{"plant": "rfcc", "plant_id": "PL-RFCC", "ts": "2026-11-19T16:19:30+09:00", "samples": [{"equip_id", "tag_id", "ts", "value"}, …]}` |
| 주기 | 측정 시각(초)마다 하나, 시각 순. 10초 태그는 그 초(0, 10, 20…초)의 메시지에만. 누락(계측 이상)이면 그 태그가 빠진다 |
| 길이 | `XADD … MAXLEN ~ 1800` (초당 1건 × 30분) |
| 보내는 범위 | 실제 시각 기준 최근 30분 안의 값만 (백필의 지난 값은 보내지 않는다) |
| 접속 | 클러스터 안 `redis://clx-redis:6379` (k8s `REDIS_URL`). Ingress 와 무관 |
| 장애 | Redis 가 느리거나 없으면 공장별 송신 스레드의 큐(30분치)에서 오래된 것부터 버리고 1분에 한 번 경고. 수신·저장은 멈추지 않는다 |

소비 쪽(Alarm Worker·탐지)은 `XREAD`(또는 `XREADGROUP`)로 다섯 스트림을 읽어 Setpoint·Nelson·ML 판정을 하면 된다. 정답(고장 모드·코드)은 싣지 않는다.

첫 화면은 `GET /stream`(같은 메시지를 공장 키 이벤트로, 연결 하나)을 구독해 브라우저 콘솔(F12 → Console)에 **Redis 로 보내는 메시지를 그대로** 남긴다 — 실시간이면 공장 5개 × 초당 1건.

### 화면에서 고장 걸기

첫 화면(공장 목록, 배포에서는 `/plant-sim/`) 아래 **고장 설정**에서 설비(공장별 묶음) → 고장 유형(그 설비에 걸 수 있는 모드) → 진행 속도(×1·×3·×5·×10) → 적용/해제를 고른다.
진행 속도는 단계 시작 시각과 ramp 상승을 1/속도로 줄인다. lag(응답 지연)·spike·wobble 모양·해제 뒤 회복(60초)은 실제 시간 그대로다(진동 주기를 줄이면 1초 표본에서 사라지고, spike 뒤에 맞춘 lag 를 줄이면 둘이 겹친다).
예: 펌프 흡입 악화의 첫 정지 ×1 34분 → ×10 3.4분. 정답지의 `stages`·`response_tags` 시각도 이 규칙으로 계산하고 `speed` 를 싣는다.
고장 유형에 "해제 후 손상 남음"이 붙은 모드는 손상 단계를 지나 해제하면 손상이 남는다. 그 설비를 고르면 **수리** 버튼이 나오고, 누르면 손상이 60초 동안 회복한다. 화면의 "손상 남음" 줄에 안 고친 사건이 모두 나온다.
적용 중인지는 서버(`/anomalies/active`, 손상은 `/anomalies/damaged`)에서 5초마다 읽으므로 다른 화면에 갔다 오거나 다른 사람이 바꿔도 "해제"로 맞게 보인다.
적용·해제·수리는 API 와 같은 토큰(`X-Ingest-Token`)이 필요하다. 화면의 토큰 칸에 넣으면 그 브라우저의 localStorage 에만 저장된다. 분리 실행에서는 안내 문구만 나온다.

### 계측 이상 (`instrument_faults`)

정상 운전에 섞이는 계측 문제 층(사용자 확정 2026-11-19, 설비 고장과 별개). 실행 설정에서 켜고, 기본은 꺼져 있다.

```yaml
instrument_faults:
  enabled: false
  spike: { per_tag_per_day: 1.0, z_min: 4, z_max: 8 }                 # 태그당 하루 평균 1회, 표본 하나가 4~8σ 튐 (부호 무작위)
  stuck: { per_equipment_per_week: 1.0, min_sec: 300, max_sec: 1800 }  # 설비당 주 1회, 태그 하나가 시작 직전 보낸 값으로 5~30분 고정
  dropout: { per_plant_per_day: 3, min_sec: 10, max_sec: 300 }         # 공장당 하루 3회, 설비 하나의 모든 태그를 10초~5분 보내지 않음
```

- 빈도·크기는 synthetic 가정이다. 일정은 `(seed, plant_id, KST 날짜)` 로만 정해져 같은 시드면 같고, Unit 을 나눠 돌려도 같다. 횟수는 포아송 분포.
- **보내는 값만** 바꾼다. 공정 값(계산 태그의 입력, 억제 표집 상태)은 그대로라 유량계가 튀어도 전류는 그대로다. 그래서 켜도 계측 이상이 걸린 표본 밖의 값은 꺼졌을 때와 같다.
- 누락은 표본을 보내지 않는다(실제 통신 끊김처럼).
- 정답지에는 수집 서버가 같은 일정을 다시 계산해 `code: 2`, `kind: 계측 이상(설비 고장 아님)`, `mode: IM-SPIKE / IM-STUCK / IM-DROPOUT`, 대상 태그(`response_tags`), `start_at`·`end_at` 으로 싣는다.
  수집 서버에 데이터가 있는 구간(가장 이른 측정 ~ `sim_now`)과 겹치는 것만 싣는다. 탐지가 이를 고장으로 알람하면 오탐이다.
- 학습 데이터: 기본은 정답지로 계측 이상 구간을 빼고, 강건성 시험으로 일부 섞어 학습할 수도 있다.

## 신호 생성

모든 값은 `(seed, seed_key, KST 날짜)` 와 공정 그룹으로 정해진다. 같은 시드로 다시 백필하면 같은 값이 나오고, Unit 을 나눠 돌려도 같다.

- 정상 신호: 태그마다 운전점 μ·σ 에 대해 `z = 공통 요인 + 개별 잡음` 을 순차로 만든다. 값 = `round(μ + σ·z, decimals)`.
  - 공통 요인: 공정 그룹(unit group)마다 초 단위 표준정규 수열 하나를 `correlation.signs` 의 신호 태그들이 나눠 갖는다(가중 `부호 × √share`, share 0.5). 평균이 0 이라 Nelson 기준을 흔들지 않고, 같은 그룹 태그끼리 상관(≈0.5)만 만든다(플랜트 단위 PCA 가 배울 구조).
  - 개별 잡음: AR(1) `e = ρ·clip(e_prev, −2, 2) + √(1−ρ²)·N(0,1)`, ρ = 0.12(온도 0.25), 가중 `√(1−share)`.
  - 억제 표집: 보낼 값이 Nelson 규칙 1~3 을 어기거나, 어기지 않는 다음 값이 남지 않게 되면(예: 8점 연속 음수이면서 증가 중) 최대 100번 다시 뽑고, 그래도 없으면 허용 구간 안의 값을 직접 정한다. 그래서 정상 구간에는 규칙 1~3 위반이 없다(탐지 쪽 Nelson 오탐률은 이 데이터로 평가할 수 없다).
    실행 설정 `quiet_normal: false`(2026-11-20, 기본 true)면 억제 표집을 하지 않는다. 정상에서도 정규분포대로 우연한 위반이 난다(RHDS 6시간 실측: 규칙 1 0.26 %, 2 0.53 %, 3 0.30 % / 표본). ML 학습·Nelson 오탐률 평가용이며, `nelson_quiet` 단계도 거르지 않는다.
  - 실행 용도 `DATA_PROFILE`(`--profile`, 2026-11-23 사용자 확정): `demo`(기본)는 runtime 파일 그대로, `eval` 은 `quiet_normal: false` 와 `instrument_faults.enabled: true` 로 바꿔 실행한다(계측 이상 빈도·크기·시드는 파일 값).
    정답지의 계측 이상은 지금 설정으로 과거 구간까지 다시 계산하므로, 용도가 다른 실행이 같은 DB 를 이어 쓰면 정답지가 틀린다. 그래서 DB(`run_meta`)에 처음 용도를 적고 다른 용도로 열면 시작하지 않는다. 이 기능 전에 데이터가 쌓인 DB 는 `demo` 로 본다.
    서버 배포는 `demo`(실시간 시연)만 쓰고 `eval` 은 로컬에서만 돌린다(사용자 확정 2026-11-23). 로컬 `eval` 은 DB 가 `data/eval/` 에 따로 생기는
    공장 목록 `config/plants.eval.yaml` 로 실행한다: `python -m collector.app --plants config/plants.eval.yaml --with-simulators --profile eval`.
    분리 실행이면 시뮬레이터에도 같은 `--profile` 을 준다.
  - 정상 구간에는 자동 이상·자동 부하 변경이 없다(D1·D2·D4). 부하 변경은 정상 운전 변화(코드 1) 모드를 주입해서 만든다.
  - 피구동 설비 `speed` 는 구동기 `speed` 의 z 를 그대로 쓴다(직결은 같은 값, 기어박스 사슬은 기어비만큼).
- 고장 모드: 정상 z 위에 효과 z 를 더한다. 억제 표집은 정상 부분만 보므로 규칙 위반은 효과 때문에만 생긴다(`nelson_quiet` 단계는 보낼 값으로 본다).
- 순차 생성이라 태그별 최근 8개 z 가 상태다. 백필 → 실시간은 같은 상태로 이어지고, 재시작 때는 `ts_raw` 에서 마지막 8개 값을 읽어 상태를 되살린다.
- 계측 이상(켰을 때): 위에서 만든 값 중 보내는 값만 튐·고착·누락시킨다.
- 표본에 상태(status) 칸은 없다. 설비가 어떤지는 탐지 파이프라인(ML 등)이 값으로 판단한다.

## 메시지 형식 v3

```json
{
  "schema_version": "3.0",
  "source_id": "SIM-RHDS",
  "batch_id": "b-20261109-172400-SIM-RHDS-0001",
  "sent_at": "2026-11-09T17:24:00+09:00",
  "samples": [
    { "equip_id": "P-3101", "tag_id": "TI-B-P3101", "ts": "2026-11-09T17:23:50+09:00", "value": 63.4 }
  ]
}
```

v3 에서 `status`(Good/Bad)를 뺐다(사용자 확정 2026-11-19: 설비 상태는 플랜트가 아니라 탐지·ML 이 판단한다). `value` 는 필수다. 다른 주 버전(v1 `quality`, v2 `status`)은 400 으로 거절한다.
수집 규칙: 같은 `batch_id` 와 같은 `(equip_id, tag_id, ts)` 는 한 번만 저장한다.
마스터에 없는 (설비, 태그) 쌍·형식 오류·보관 기간 밖의 행은 거절하고 응답에 건수와 사유를 돌려준다. 시각 순서가 뒤바뀐 행도 받는다. v3.x 에서 추가된 필드는 무시하고 받는다.

## API

루트와 공장 접두어 아래의 두 층이다. 공장 접두어는 `config/plants.yaml` 의 `key`.

| 메서드 · 경로 | 용도 |
| --- | --- |
| `GET /` | 공장 목록 페이지 |
| `GET /health` | 전체 상태. 공장마다 `plant_id`, `sim_now`, `last_received_at`, `catching_up`, `live`(백필이 끝나 실시간인지) |
| `GET /{공장}/` | 디버깅 화면. 설비별 표에 태그의 값·측정 시각이 SSE 로 1초마다 갱신된다. 한계값을 넘은 값은 행을 칠한다(경고 노랑·알람 연빨강·정지 빨강 — 판정이 아니라 보기용) |
| `POST /{공장}/ingest` | 시뮬레이터 묶음 수신 |
| `GET /{공장}/health` | 그 공장의 상태 |
| `GET /{공장}/equipment` | 계층 순서의 설비 목록(`driven_by`·`operating`)과 태그(설명·단위·주기·자릿수·한계값(참고)·`normal_ref`·`limit_basis`·운전점을 반영한 `control` μ/σ) |
| `GET /{공장}/current` | 모든 태그의 현재값(`ts`·`value`·`received_at`). 아직 값이 없는 태그는 `null` |
| `GET /{공장}/equipment/{equip_id}/tags/{tag_id}/current` | 태그 현재값. 오래됐는지는 `ts` 로 본다 |
| `GET /{공장}/timeseries?equip_id=&tag_id=&from=&to=&interval=1h` | 집계 시계열 (`10m` / `1h`) |
| `GET /{공장}/fault-modes` | 걸 수 있는 고장 모드와 대상 설비 |
| `GET /{공장}/flow` | 공정 흐름 그래프(가상 공장): `{plant, nodes, equipment: [{equip_id, driven_by}], edges: [{from, to, label, stream}]}`. 3D 뷰어가 하류 영향 표시에 읽는다 |
| `POST /{공장}/anomalies` | 설비에 고장 모드 주입 (토큰 필요, 통합 실행 전용) |
| `POST /{공장}/anomalies/{equip_id}/stop` | 그 설비의 사건 멈춤 |
| `POST /{공장}/anomalies/stop` | 공장의 사건 전부 멈춤 |
| `POST /{공장}/anomalies/{equip_id}/repair` | 그 설비의 남은 손상 수리 |
| `GET /{공장}/anomalies?equip_id=&from=&to=` | 정답지 (고장 모드·분류·단계·태그별 효과 시작) |
| `GET /{공장}/stream` | SSE 로 새 값 실시간 전달 (이벤트 `samples`, 받은 묶음마다 하나 — 실시간이면 초마다 그 초의 태그 전체) |
| `GET /stream` | 모든 공장의 초 단위 메시지를 연결 하나로 (이벤트 이름 = 공장 키, 데이터 = Redis Stream 의 `data` 와 같은 `{plant, plant_id, ts, samples}`). 공장마다 연결을 열면 HTTP/1.1 의 호스트당 연결 한도(6)를 거의 다 쓴다 |

`from`/`to` 는 ISO 8601 이다. 쿼리 문자열의 `+` 는 공백이 되므로 `Z`(UTC) 를 쓰거나 오프셋을 `%2B09:00` 으로 인코딩한다. 읽지 못하면 400.

Nelson 알람(`/alarms`, `nelson_alarm`)과 Redis Stream `plant-sim:alarms` 는 2026-11-19 에 지웠다. 이상 판정·알람은 탐지 파이프라인이 Redis Stream `plant-sim:data:{공장}`(위) 이나 이 API 의 값으로 한다.

## DB (`data/{공장}.db`)

스키마 v5 (status 칸 없음). 시각 컬럼은 모두 UTC ISO 문자열이고 태그는 `(equip_id, tag_id)` 복합키다. 배치는 컴퓨터 시계가 아니라 수신한 측정 시각의 최대값(`sim_now`)을 보고
그 시각이 지난 구간을 처리하므로, 백필에서도 실시간과 같은 경로로 집계가 만들어진다.

| 테이블 | 채워지는 경로 |
| --- | --- |
| `ts_raw`, `tag_current` | 수신 즉시 |
| `ts_10m`, `ts_1h` | 10분·매시 배치. 평균·최소·최대·표준편차는 모든 표본으로, 표본이 없는 구간은 `count` 0 |
| `anomaly_event` | 정답지 겸 생성기 상태 (주입한 고장 모드. `plan` 에 주입 당시 단계·태그별 효과 시작) |
| `plant`, `unit_group`, `unit`, `equipment`, `equipment_type`, `equipment_alias`, `tag_std`, `equipment_tag` | 구성안에서 서버 시작 때 |
| `ingest_batch`, `job_state`, `schema_meta` | 중복 확인, 배치 진행 상태, 스키마 버전 |

- **예전 DB 파일(v1~v4)은 열 수 없다.** 수집 서버가 파일 경로와 함께 옮기거나 지우라고 알리고 멈춘다. `data/old/` 로 옮긴 뒤 백필을 다시 돌린다. 배포(k8s)에서는 DB v5 이므로 PVC 의 기존 DB 파일을 지우고 다시 띄운다.
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

- 이미지는 Harbor 비공개 프로젝트(`harbor.uru42.space`)에 있으므로 Deployment 에 `imagePullSecrets: uru42-harbor-pull` 을 적어 둔다(네임스페이스에 그 Secret 이 있어야 한다: `kubectl -n u4-2 get secret uru42-harbor-pull`). 같은 네임스페이스의 `harbor-cred` 는 `harbor.skala-us.com` 인증이라 이 이미지를 받지 못한다(ImagePullBackOff "no basic auth credentials").
- **스키마 버전이 바뀐 배포** (2026-11-20부터): 배포 매니페스트의 `RESET_DB_ON_SCHEMA_CHANGE=1` 로, 버전이 다른 DB 파일(`/app/data/*.db*`)은 시작할 때 지우고 새로 만든다. 하루치 재백필(약 40분)이 따라오고, 이전 정답지(`/anomalies`)·집계도 함께 지워진다(남겨야 하면 배포 전에 API 로 내보낸다). 스키마가 같은 배포는 기존 DB 에서 이어 간다.
  이 변수가 없으면(로컬 기본) 예전 파일은 열지 않고 알리며 멈춘다. 더 새 버전 파일(예전 이미지로 되돌린 경우)도 같은 방식으로 거부하거나 지운다.
- 첫 기동은 다섯 공장 하루치 백필이다. CPU 500m 에서 약 40분 걸리며(아래 실측) 그동안 `/health` 의 `sim_now` 가 전진하고 `live` 는 false 다. 파드는 `/health` 가 응답하면 Ready 가 되므로 백필 중에도 조회 API 는 쓸 수 있다.
- 백필 기간을 늘리려면(`--backfill-days`) PVC 의 DB 파일을 지우고 다시 채워야 한다. 재개는 마지막 시각부터만 이어 간다.
- 다른 파드는 DB 파일을 직접 읽을 수 없다(RWO). HTTP API(`/current`, `/timeseries`, `/anomalies`, `/stream`)를 쓴다.
- 실측 (다섯 공장 통합 실행, 하루 백필):

  | 조건 | 생성기 | 백필 중 `/health` 응답 | 하루 백필 → 실시간 | 메모리 |
  | --- | --- | --- | --- | --- |
  | 로컬 Mac, 제한 없음 | Nelson (지금) | — | 약 7.3분 | Python RSS 약 243 MB |
  | k8s `u4-2` 배포 한도(500m/1Gi), 새 PVC | Nelson (지금) | 200 (readiness 통과) | 약 38분 (RHDS·VRDS·CDU·HOU 37~38분, RFCC 38~39분) | `kubectl top` 710Mi (SQLite 파일 캐시 포함), 실시간 CPU 약 60m |
  | 로컬 Mac, 제한 없음 | 이전 신호 모델 | 0.1초 이하 | 약 7분 | Python RSS 약 250 MB |
  | `docker run --cpus 0.5 --memory 1g` (배포 한도) | 이전 신호 모델 | 0.1~3.7초, 대개 2~3초 (CPU 쿼터를 백필과 나눠 쓴다) | 약 15분 | cgroup 기준 785~812 MiB (SQLite 파일 캐시 포함, 회수 가능) |

  k8s 수치는 2026-11-13 배포(이미지 `test-8-254597bb`)에서 1분 간격으로 `/health` 를 보고 잰 것이라 ±1분이다(`live` = 백필 뒤 밀린 구간까지 따라잡은 시각). 1시간 생성은 Mac 에서 공장당 1.4~2.4초다.
  순수 파이썬 생성기가 `/health` 를 막지 않도록 통합 실행 중에는 GIL 전환 간격을 1ms 로 줄인다.
  2026-11-19 생성기(규칙 1~3 판정)는 같은 Mac 에서 RFCC 기준 약 1.7배 빠르다(98k → 167k 표본/초, 생성만). 배포 실측은 아직 다시 하지 않았다.
  readiness·liveness 의 timeout 은 10초, startupProbe 는 30분이다(백필 중에도 `/health` 가 응답하므로 백필이 30분을 넘어도 파드는 먼저 Ready 가 된다). 백필이 끝나면 응답은 수 ms 로 돌아온다. 더 빨리 띄우려면 limits cpu 를 1 로 올린다.

## 폴더

```text
config/      plants.yaml · plant_*.yaml · runtime_*.yaml · fault_modes.yaml
common/      config_models(복합키·한계값·control·운전점·공통 요인) · message(v3) · nelson(규칙 1~3 판정·허용 구간) · sim_clock · timefmt
simulator/   model(정상 신호 + 계산 태그 + 고장 모드 효과) · fault(고장 모드 정의·태그별 적용·효과 모양) · instrument(계측 이상 일정) · sender · main
collector/   app(라우트) · plants(공장 목록) · plant_context(공장별 문맥·수신 핵심) · embedded(통합 실행) · settings · ingest · anomaly_book(고장 모드 사건 장부·정답지) · storage · schema.sql · jobs · seed_master · stream · static/index.html(현재값) · static/fault_panel.html(첫 화면 고장 설정) · static/stream_log.html(첫 화면 브라우저 로그) · data_stream(측정값 Redis Stream)
deploy/k8s/  kustomization · plant-sim-{deployment,service,pvc,ingress}.yaml · plant-sim-secret.example.yaml (being-gitops k8s/plant-sim 으로 복사)
docs/        superpowers/specs (RFCC · 다중 공장 · k8s · Nelson 이상 설계(대체됨)) · superpowers/plans (구현 계획 · Nelson 작업 기록 · 시나리오 주입형 전환)
tests/       판정기·고장 모드·생성기·사건 장부·수집·이상 API·설정·배포 매니페스트 테스트
```
