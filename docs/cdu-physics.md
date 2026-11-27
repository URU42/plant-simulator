# CDU 원유 공급 펌프 물리 재생

> 2026-11-25 PU-D5a: 원료 펌프가 두 대(CDU·RFCC 병렬, HOU·RHDS·VRDS 직렬)로 늘었다. 이 문서의 단일 펌프 회로 수치·검증은 그 전 기록이다. 현재 회로는 `config/physics_*_integrated.yaml` 의 `station` 과 README 의 '두 번째 원료 펌프' 절을 본다.

`UNIT-CDU-01`의 원유 공급 펌프 `P-8101A`에서 탈염기 방향 `DS` 경계까지 계산한다. 설비·신호·센서 태그와 `process` 연결은 기존 `config/plant_cdu.yaml`에서 확인한다. `DS`는 토출 배관 끝의 압력 경계다. 오프라인 재생은 기존 수집·AI API에 접속하거나 데이터를 적재하지 않는다.

## 실행

저장소 루트에서 실행한다. 기존 Python 프로젝트 의존성과 진단 저장소 Frontend의 잠금 의존성(Three.js · Vite, Node 22.12 이상)이 설치되어 있어야 한다. `cdu-run`은 새 경로여야 한다. Windows PowerShell에서는 설치 환경의 Python 명령에 따라 `python`을 `py -3`으로 바꿀 수 있다.

```sh
python -m simulator.physics_demo cdu-run
node demo/cdu/build.mjs ../sk-clx-diag-agent/apps/frontend cdu-run
python -m http.server 8765 --bind 127.0.0.1 --directory cdu-run/public
```

브라우저에서 `http://127.0.0.1:8765/`를 연다. 3D 모형은 드래그로 회전하고 휠로 확대할 수 있다. 슬라이더나 재생 버튼으로 수신 프레임을 탐색하고 GLB를 다운로드한다. 빌드한 `public/`은 정적 파일로 제공할 수 있다. macOS와 Windows 모두 브라우저 WebGL을 사용하고 계산은 CPU에서 실행된다. 별도 GPU 서버, Omniverse, CDN 또는 진단 백엔드는 실행 경로에 필요하지 않다.

`--normal`은 흡입 압력 고장 주입을 끈다. 두 모드 모두 계측 분리를 확인하기 위해 40~43초의 합성 누락을 포함한다.

## 물리 모델과 단위

| 항목 | 기준 |
| --- | --- |
| 시간 | 2026-11-24 00:00:00 KST (+09:00)에서 시작. 물리·대상 센서 1초 격자, 75초 계산, 계측 배달 2초 지연 |
| 압력 | 내부 Pa(abs). 계측은 기존 태그 표기 `kg/cm2(g)`를 따르며, kgf/cm²(g)를 뜻한다. 1 kgf/cm² = 98,066.5 Pa. Pa(abs) = Pa(gauge) + 101,325 Pa |
| 유량 | 내부 m³/s, 태그 `FI-8101A`는 m³/h. 밀도 850 kg/m³ |
| 온도·에너지 | 유체 입구 313.15 K, 정압 비열 2,100 J/(kg·K). 동력과 에너지 수지 잔차 W. 태그 `II-8101A`는 기존 유량×차압 파생식에 따른 추정 전류 A |
| 펌프 | 고정 회전수의 2차 차압 곡선. 체절 차압 3.10 MPa, 기준 유량 0.11148554 m³/s에서 약 2.453 MPa, 효율 75% |
| 토출 배관 | 길이 70 m, 내경 0.25 m, Darcy 마찰계수 0.025, 국부 손실계수 합 8, 출구 경계 2.796 MPa(abs) |

`config/physics_cdu.yaml`의 성능곡선·효율·물성·경계압·배관 치수와 Three.js 배치·노즐 좌표는 **synthetic assumption**이다. 기존 합성 태그의 정상 운전점을 맞춰 초기 유량과 게이지 압력을 정했다. GLB는 가시화용 모형이며 좌표와 시각적 배관 길이는 계산 배관을 축척으로 재현하지 않는다.

펌프 차압과 Darcy–Weisbach 손실을 각각 유량의 제곱 함수로 두고 교점을 해석적으로 푼다. 계산한 펌프 유량과 펌프 토출압으로 다시 구한 배관 출구 유량에 밀도를 곱해 질량 유량을 비교한다. 같은 고도·입출구 운동에너지 차 0·축열 0·단열을 가정한다. 펌프 효율 손실과 배관 마찰 손실은 유체 가열로 처리한다. 에너지 수지는 축동력에서 압력 일과 유체의 현열 증가를 뺀 값으로 확인한다. 대상 운전점 테스트 허용치는 질량 10⁻⁹ kg/s, 에너지 10⁻⁶ W다.

## 계측·정답 경계

- 고장 엔진의 `PM-SUCTION-LOSS` 시각·상승·회복을 재사용한다. 흡입 압력 효과만 경계 조건에 연결하고 나머지 신호는 수력 계산과 전류 파생식에서 얻는다. 기존 모드의 진동·온도 반응을 이 재생에 포함하지 않는다.
- 시드 기반 계측 잡음과 반올림은 물리 계산이 끝난 다음 적용한다. 샘플의 `ts`는 측정 시각이고 프레임의 `at`는 배달 시각이다. 누락 구간은 해당 표본을 내보내지 않는다. 수신 실험용 잡음 σ는 기존 태그 기준을 사용하며 전류는 파생식의 `noise_sigma`를 사용한다.
- `public/measurements.json`은 `Sample` v3 형식의 계측값과 표시용 메타데이터를 담는다. `private/truth.json`은 물리 상태, 입력 설정, 주입 ID와 계측 누락 일정을 담는다. 배포·공유·HTTP 서빙은 `public/`만 대상으로 한다. 기존 수집 서버의 `/anomalies`는 별도의 기존 운영 경로다.

## 검증과 다음 입력

```sh
python -m pytest -q
node --test demo/cdu/build.test.mjs
```

Node 테스트에서 Python을 지정하려면 `PYTHON`, 다른 Frontend 경로를 지정하려면 `CDU_DEMO_FRONTEND`를 사용한다. 진단 저장소에서 `python scripts/check_contracts.py`로 공통 API 계약을 확인한다.

준정상 단상·비압축성 모델이다. 배관 충전이나 열 축적, 역류·무유량 운전, NPSH/캐비테이션, 기동·정지, 제어 루프, 진동·베어링 온도는 추가 모델이 필요하다. 다른 Unit의 동종 회로는 기존 설비·태그·공정 간선과 새 `PumpCircuit` 설정을 매핑할 수 있다. 분기·다중 펌프는 별도의 네트워크 수지가 필요하다.

다음 단계에 필요한 입력은 사용 권한이 있는 펌프 Q–H·효율·NPSHr 곡선, 배관 목록(ID·직경·길이·고도·피팅·밸브), 유체의 온도별 밀도·비열·점도·증기압, 흡입·토출 경계 조건, 태그 단위·계측 지연과 설비/노즐 좌표 또는 승인된 GLB다. 실제 진단 연동 시에는 평가용 정답 저장소와 서비스 계정의 접근 경계도 확인해야 한다.

## D1 정상 유압 네트워크

D1 CLI는 `config/physics_cdu_network.yaml`의 회로를 푼다. 기존 3D 재생은 자체 설정과 단일 분기 에너지 모델을 유지하며, 유압 교점 계산을 D1 모듈과 공유한다.

### 설비·센서 매핑

`config/plant_cdu.yaml`의 `UNIT-CDU-01` 소속, 진단 저장소 `ontology/source.sql`의 `asset`·`sensor_tag`·`topology_edge`, `ontology/simulator-sensor-tags.csv`의 신호·단위·주기를 대조했다. 설비의 온톨로지 키는 `CDU_P-8101A`이고 센서 키는 `CDU_P-8101A/<tag_code>`다. 아래의 `tag_code`는 기존 측정 메시지 `tag_id`와 같은 값이다.

| equip_id | tag_code | 온톨로지 신호 | 기존 단위 / 주기 | 물리 모델 매핑 |
| --- | --- | --- | --- | --- |
| P-8101A | PI-8101A | suction_pressure | kg/cm2(g) / 1초 | 입력 `suction_abs_pa`에서 대기압을 빼고 98,066.5 Pa로 나눈 값 |
| P-8101A | PI-8101B | discharge_pressure | kg/cm2(g) / 1초 | 출력 `discharge_abs_pa`에 같은 변환 적용 |
| P-8101A | FI-8101A | flow | m3/h / 1초 | `pump_flow_m3_s × 3600`. 분기 전 펌프 전체 유량 |
| P-8101A | II-8101A | current | A / 1초 | 기존 단일 분기 재생의 `derive`가 전체 유량과 펌프 차압으로 계산. D1 정상점 JSON은 수력·축동력 W를 제공 |
| P-8101A | VI-8101A | vibration | mm/s RMS / 1초 | 기존 태그 보존, D1 미계산 |
| P-8101A | VI-8101B | vibration | mm/s RMS / 1초 | 기존 태그 보존, D1 미계산 |
| P-8101A | TI-8101A | bearing_temp | DEG C / 10초 | 기존 태그 보존, D1 미계산 |

기존 공정 간선 `CDU/flow/1`은 `P-8101A → DS`다. `DS`는 온톨로지 공정 노드 `CDU/process/DS`이며, D1은 그 위치를 고정 압력 경계로 사용한다.

가상 물리 노드는 `virtual:suction-boundary`, `virtual:discharge-junction`, `virtual:bleed-pressure-boundary`다. 두 분기, 배관, 밸브도 `virtual:` 이름 공간을 사용한다. 보조 분기는 펌프 토출 유량을 나누는 합성 분기이며 원본 공정 간선에 추가하지 않는다. 흡입 탱크의 유체 재고나 액위는 계산하지 않는다.

### 운전점 명령과 JSON 형식

```sh
python -m simulator.physics
python -m simulator.physics --config config/physics_cdu_network.yaml --point point.json
python -m simulator.physics --schema config
python -m simulator.physics --schema point
python -m simulator.physics --schema result
```

`point.json` 예제는 다음과 같다.

```json
{
  "suction_abs_pa": 380000,
  "speed_rpm": 3650,
  "valve_openings": {
    "virtual:main-valve": 0.9,
    "virtual:bleed-valve": 0.7
  }
}
```

흡입 압력과 회전수는 유한한 양수다. 밸브 개도는 무차원 `0 < opening ≤ 1`이며, 입력에서 생략한 밸브는 완전히 열린다. `--point`를 생략하면 설정의 `default_point` 전체를 사용한다. 출력 상위 객체에는 `plant_id`, `unit_id`, `equip_id`, `suction_node`, `discharge_node`, `provenance`, 유효한 입력 `operating_point`, 계산 결과 `result`가 포함된다. 입력 오류는 stderr와 종료 코드 2로 반환한다.

`result`에는 흡입·토출 절대압 Pa, 회전수 rpm, 펌프 전체 유량 m³/s, 양정 m, 수력·축동력 W, 질량 잔차 kg/s, 펌프 압력 잔차 Pa가 있다. 각 `branches[]` 항목의 `branch_id`와 `outlet_node`는 구성 식별자이고, `outlet_abs_pa`, `friction_loss_pa`, `elevation_pressure_pa`, `pressure_residual_pa`는 Pa, `flow_m3_s`는 m³/s다. `--schema`가 실제 검증 모델의 JSON Schema를 출력한다. 이 정상점은 시간축이 없는 계산 결과다. 기존 `Sample` v3 재생의 KST 측정·수신 시각과 1초 격자는 앞 절을 따른다.

### 파라미터와 계산 범위

| 구성 | 파라미터 |
| --- | --- |
| 유체 | `density_kg_m3` kg/m³, 비압축성·일정 밀도. 중력 가속도 9.80665 m/s² |
| 펌프 | `reference_speed_rpm` rpm, `rated_flow_m3_s` m³/s, `shutoff_head_m`·`rated_head_m` m, `efficiency` 무차원 |
| 배관 | `length_m`·`diameter_m` m, `darcy_factor`와 `minor_loss_k` 무차원 |
| 밸브 | `diameter_m` m와 `full_open_k` 무차원. 개도에 비례해 유효 단면적이 작아진다고 가정 |
| 분기 경계 | `outlet_abs_pa` Pa(abs), 토출 분기점 대비 출구의 `elevation_change_m` m. 출구가 더 높으면 양수 |

기준 속도의 체절·정격점을 통해 `H(Q) = H0 − aQ²`의 계수 `a`를 정한다. 속도비 `r = N/Nref`에서 `H(Q,N) = r² · H(Q/r,Nref)`를 적용한다. 배관 손실은 `(fL/D + K)ρv²/2`, 밸브 손실은 `Kopen ρ(Q/(A·opening))²/2`다. 분기마다 직렬 손실과 고도에 따른 `ρgΔz`를 더한다.

토출압이 커지면 펌프의 공급량은 감소하고 병렬 분기의 수요는 증가한다. 공급과 수요가 같은 압력을 이분법으로 계산해 분기별 압력 수지와 질량 잔차를 출력한다. 이 단조 문제는 기존 의존성 구성에서 표준 라이브러리만으로 풀 수 있어 SciPy를 추가하지 않았다. 설계 곡선·효율·배관·고도·밸브·회전수·경계압은 **synthetic assumption**이다. 모델 범위는 펌프 1대, 토출 분기점 1곳, 분기별 직렬 손실이다. 역류, 완전 폐쇄, 다중 분기점·루프, 레이놀즈 수에 따른 마찰계수, NPSH 및 과도응답은 별도 모델이 필요하다.

## D2-A 기존 생성기와 결합

(2026-11-25 PU-D5a: `runtime_cdu.yaml`은 이제 `physics`와 `physics_general: true`로 물리를 켠다.) 실행 설정에 다음 항목을 두면 기존 생성기가 CDU 결합 회로를 사용한다. 경로는 런타임 YAML의 디렉터리를 기준으로 해석한다. 설정을 생략하거나 `null`로 두면 기존 통계형 생성 경로를 사용한다.

```yaml
physics: physics_cdu_integrated.yaml
```

통합 실행은 아래 명령을 사용한다. 모드별 백필·수집 비교에는 별도 DB 경로를 지정한 plants YAML을 사용한다. 같은 DB에 두 생성 경로를 섞으면 기존 표본과 고장 적용 시점의 기준이 달라진다.

```sh
uv run python -m collector.app --plants config/plants.yaml --with-simulators --time-mode live
uv run python -m simulator.physics_scenario cdu-scenario-example
```

기존 주입 요청 `POST /cdu/anomalies`에 `equip_id: P-8101A`와 아래 `mode`를 사용한다. 기존 `speed` 값의 의미도 같다. 멈춤·수리에는 각각 `POST /cdu/anomalies/P-8101A/stop`, `POST /cdu/anomalies/P-8101A/repair`를 사용한다. 쓰기와 물리 모드의 진행·손상 조회에는 운영자 `X-Ingest-Token`이, 정답지 `GET /cdu/anomalies`에는 별도 `X-Eval-Token`이 필요하다.

| 모드 | 물리 파라미터 | 복귀 |
| --- | --- | --- |
| `PM-SUCTION-LOSS` | S1·S3에서 합성 스트레이너의 흡입 저항 (R) 증가. 흡입원과 펌프 사이 압력 손실은 (RQ²) | 정지 뒤 60초에 걸쳐 저항 복귀 |
| `PM-IMPELLER-WEAR` | 체절·정격 양정과 효율 감소. 합성 최대 양정 감소 10%, 효율 상대 감소 20% | 정지 뒤 유지, 수리 뒤 60초 동안 복귀 |
| `PM-DISCHARGE-VALVE-RESTRICTION` | 제품 분기 제어밸브 개도 감소. 최대 변화에서 기본 개도의 40%, 저항은 개도의 제곱에 반비례 | 정지 뒤 60초 동안 개도 복귀 |
| `OP-THROUGHPUT-CHANGE` | 같은 제어밸브의 작은 개도 감소로 정상 운전 변화 구현 | 정지 뒤 60초 동안 복귀 |

기존 고장 엔진의 단계 시각·ramp·정지·수리를 사용한다. 물리 모드에서 전용 마모·밸브 모드는 `P-8101A`에만 표시된다. 물리 파라미터가 없는 모드는 연결된 압력·유량·전류에 z-score를 더하지 않는다. 같은 파라미터에 사건이 겹치면 시각별로 가장 큰 적용량을 사용한다.

공통 토출 헤더에서 다른 제품 등가 분기와 `DS → HX → HT → AT → P-8401A` 제품 등가 분기의 유량을 동시에 푼다. 후속 펌프의 Q–H와 토출 배관 손실도 수지에 참여한다. 헤더 압력에 따라 각각의 분기 유량과 `P-8401A` 토출압이 달라진다. 제품 분리에 따른 물성·조성 변화, 공정 열수지, 이송 지연을 계산하지 않으며 단일 합성 밀도와 등가 저항을 사용한다. 물리 사건의 공정 하류 z-score 전파는 사용하지 않는다. 결합 회로 밖 설비에 대한 기존 경험적 효과와 축·유틸리티 효과는 물리 계산값과 별개다.

- `P-8101A`의 `PI-8101A`, `PI-8101B`, `FI-8101A`와 `P-8401A`의 `PI-8401B`, `FI-8401A`는 수력 해가 기준값이다. `II-8101A`, `II-8401A`는 각각 정상 전류에 정상 대비 축동력 비를 곱한 대용 계측값이다. 전압·역률·전동기 제어 모델은 범위 밖이다. 양정·동력 전용 태그는 마스터에 추가하지 않았으며, 계산값은 평가용 JSON의 m·W 단위 상태에서 볼 수 있다.
- 압력·유량은 기존 시드 기반 공통·AR 잡음을, 전류는 기존 파생 태그의 자체 잡음 폭을 사용한다. 일반 태그는 1초, 베어링 온도는 10초 격자다. 진동·베어링 온도의 기존 고장 효과는 경험 신호이며 캐비테이션·구조 진동·베어링 열전달의 해로 해석하지 않는다. 기존 계측 이상 스케줄은 계산 다음에만 적용한다.
- 수집·현재값·SSE는 기존 v3 계측 형식을 유지한다. 평가용 사건·분류·응답 태그는 기존 사건 장부에 저장한다. 물리 모드에서 `GET /anomalies`, `/anomalies/active`, `/anomalies/damaged`는 평가·운영자 토큰으로만 열 수 있다. 진단 클라이언트에는 이 토큰과 평가 파일을 제공하지 않는다. 기존 통계형 모드의 조회 권한은 기존 설정을 따른다.
- 로컬 생성물 `examples/cdu-physics/observations/measurements.csv`는 두 사건의 주요 시점에서 뽑은 계측 표본이다. `examples/cdu-physics/evaluation/truth.json`은 같은 시점의 실제 합성 상태와 기존 사건 장부의 정답이다. 막힘은 10초에 시작해 80초에 멈추고 140초에 회복한다. 마모는 160초에 시작해 200초에 멈춘 뒤에도 남으며, 220초에 수리해 280초에 회복한다. CSV를 진단 입력으로 사용하고 JSON은 별도로 평가에 사용한다.


## D4 로컬 실시간 검수

2026-11-24부터 `scripts/prepare_twin.py` 가 다섯 공장 설정을 만들고(`prepare_cdu_twin.py` 는 CDU 만), 브라우저 E2E 는 진단 저장소 `scripts/twin_browser_e2e.mjs` 를 쓴다. 다섯 공장 범위는 [plant-twin-d4.md](plant-twin-d4.md).

`sk-clx-diag-agent`의 3D CDU 화면에서 운전·정상 전환·배속·막힘을 조작한다. 실제 배치 인증이나 고장 검출 정확도 검증에는 사용하지 않는다. 합성 관측·정답·검수 로그와 비밀번호는 Git에서 제외하며 프로젝트의 `.cache/d4`에만 보관한다. D2 예제 출력 `examples/cdu-physics`도 로컬 생성물이다.

먼저 새 로컬 설정을 생성한다. 아래의 `D4_RUN`에는 출력된 **절대 경로**를 사용한다. 각 서비스는 별도 터미널에서 조작 없이 기동한다.

```sh
uv run --locked python -m scripts.prepare_cdu_twin
export D4_RUN='/absolute/path/from/output'
set -a; source "$D4_RUN/secrets.env"; set +a
```

1. `plant-simulator`에서 collector를 `SIMULATION_CONTROL=1 uv run --locked python -m collector.app --plants "$D4_RUN/plants.yaml" --with-simulators --host 127.0.0.1 --port 8010`으로 실행한다.
2. 진단 저장소의 AI 서비스 환경에서 `TWIN_MOCK_ENABLED=1`과 위의 내부 토큰을 설정하고 `python -m uvicorn app.main:app --host 127.0.0.1 --port 8011`로 실행한다. 기존 AI 의존성을 사용한다.
3. 진단 Backend를 데모 전용 H2 설정으로 실행한다. Java 21에서 기존 Gradle Wrapper의 `bootRun`을 사용한다.

   ```sh
   export SERVER_PORT=8081 SERVER_ADDRESS=127.0.0.1 TWIN_ENABLED=true
   export TWIN_COLLECTOR_URL=http://127.0.0.1:8010 TWIN_COLLECTOR_TOKEN="$INGEST_TOKEN"
   export AUTH_COOKIE_SECURE=false AI_BASE_URL=http://127.0.0.1:8011
   export DB_URL='jdbc:h2:mem:cdu-d4;DB_CLOSE_DELAY=-1' DB_USERNAME=sa DB_PASSWORD=
   export SPRING_FLYWAY_LOCATIONS=classpath:db/migration
   ./gradlew bootRun
   ```

4. Frontend에서 `BACKEND_PROXY_URL=http://127.0.0.1:8081 npm run dev -- --port 5189`를 실행하고 `http://127.0.0.1:5189/3d-view?plant=CDU`를 연다. 로컬 설정의 승인 계정으로 로그인한다.

새 collector 인스턴스의 준비 상태에서 아래 명령은 시작→일시정지·재개→SSE 재연결→막힘·후속 변화→mock 진단→정상 복귀를 검증한다. 동일 조작 ID의 재시도와 변경된 본문 충돌도 검사한다. E2E가 끝나면 회복한 상태에서 일시정지하므로 화면에서 재개할 수 있다.

```sh
D4_USERNAME="$AUTH_ADMIN_USERNAME" D4_PASSWORD="$AUTH_ADMIN_PASSWORD" \
  uv run --locked python scripts/cdu_twin_e2e.py --output "$D4_RUN/e2e.json"
```

새로 열린 SSE는 최신 스냅샷으로 복구하며 중간에 놓친 표본을 재생하지 않는다. 조작 중복 제거는 프로세스 내 최근 1,024건에 한한다. 진단 모드는 `mock`이며 정답 ID를 읽지 않는다. 실측 추론과 외부 배포는 이 로컬 경로에서 실행하지 않는다.
