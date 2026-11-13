# Skadi 앞단 데이터 생성기 기획서

Nov 8, 2026 · @xxhigh

## 1. 개요

가상 공장 다섯 곳의 센서 값을 끊임없이 만들어 Skadi 프로젝트 DB에 쌓는 앞단 프로그램(시뮬레이터 + 수집 서버)을 만든다. 이 문서는 Claude Code에서 구현을 이어 가기 위한 기획서다.

- **목적**: 트렌드 분석, 설비 히스토리, 트러블슈팅을 제공하는 AI Agent가 읽을 센서 데이터와 이상 징후를 팀이 직접 만든다. 이상 사건의 정답지(원인·분류·조치)도 같은 생성기가 함께 만든다.
- **범위**: 설비 시뮬레이터, 중앙 수집 서버, 프로젝트 DB에 쓰는 데까지다. Skadi 화면과 Agent는 범위 밖이며 DB를 읽기만 한다.
- **위치**: 실제 공장의 현장 센서와 RTDB(센서 값을 모으는 실시간 DB) 역할을 흉내 낸다. 그래서 시뮬레이터를 실제 데이터로 바꿔도 Skadi 쪽은 고칠 게 없다.

### 확정·추천 사항

| 항목 | 내용 | 상태 |
| --- | --- | --- |
| 대상 공장 | 다섯 곳: RFCC·CDU·HOU·RHDS·VRDS (설비 38대, 태그 274개). 공장 목록은 `config/plants.yaml` | 확정 |
| 생성·전송 주기 | 값은 1초마다(온도 태그는 10초), 전송도 1초 묶음. 주기는 `runtime_*.yaml` | 확정 |
| 전송 수단 | HTTP POST. 전송부를 분리해 나중에 MQTT로 교체 가능 | 확정 |
| 과거 데이터 | 같은 생성기로 지난 하루(`backfill_days`)를 백필한 뒤 실시간으로 이어 간다 | 확정 |
| 저장 | 별도 DB 서버 없이 공장마다 SQLite 파일 하나 | 확정 |
| 실행 형태 | 통합 실행(수집 서버가 생성기까지 한 프로세스, 기본)과 분리 실행(수집 서버 + 공장별 시뮬레이터) 둘 다 지원 | 확정 |
| 시뮬레이터 단위 | 한 프로세스로 시작하되, 코드는 Unit 단위로 나눠 실행할 수 있게 만든다 | 확정 |
| 구성안 교체 | 엔진 하나, 구성안 설정 파일만 교체 (공장 추가는 파일 추가와 `plants.yaml` 한 줄) | 확정 |
| 이상·알람 | 정상 신호는 고정 μ·σ, 이상은 Nelson 8종 규칙으로 만들고 판정한다 (5장) | 확정 |

## 2. 시스템 구조와 기술 스택

앞단은 ‘값을 만드는 쪽’과 ‘받아서 저장하는 쪽’으로 나뉘고, 공장마다 SQLite 파일 하나에 저장한다. 통합 실행에서는 두 쪽이 한 프로세스에서 돌고, 분리 실행에서는 프로세스가 둘로 나뉜다.

```text
[설정 파일: plants.yaml → plant_X.yaml · runtime_X.yaml] × 5
        │
        ▼
[설비 시뮬레이터]  ── HTTP POST /{공장}/ingest ──▶  [중앙 수집 서버 (FastAPI)]
  공장별 생성기      (통합 실행: 같은 프로세스에서                │  저장 · 집계 · Nelson 판정
  1초마다 값 생성     LocalSender 로 직접 넣는다)                  ▼
                                                      [프로젝트 DB (SQLite, 공장당 1개)]
                                                                  ▲
                                                     [Skadi 화면 · AI Agent] (읽기만)
```

| 역할 | 기술 | 이유 |
| --- | --- | --- |
| 설비 시뮬레이터 | Python, asyncio, NumPy | 공장마다 생성기를 비동기 작업으로 돌리고, NumPy로 정상 신호와 이상을 만든다 |
| 설정 파일 | YAML + Pydantic | 구성안을 파일로 갈아 끼우고, 시작할 때 잘못된 설정을 걸러 낸다 |
| 전송 | httpx (HTTP POST) | 묶음 전송. 전송부를 분리해 두면 나중에 MQTT로 바꿀 수 있다 |
| 중앙 수집 서버 | FastAPI + Uvicorn | 수집 API, 조회 API, 실시간 스트림을 한 서버에서 제공하고, 공장 목록 파일 하나로 여러 공장을 `/{공장}/…` 아래에 같은 모양으로 연다 |
| 저장 | SQLite (WAL 모드) | 서버 없이 파일 하나로 동작한다. 원시 값은 `raw_retention_days` 만큼만 보관한다 |
| 집계·보관 배치, Nelson 판정 | FastAPI 안의 백그라운드 작업, 판정은 받을 때마다 | 같은 프로세스에서 돌아 별도 스케줄러가 필요 없다 |
| 실시간 화면 갱신 | SSE (Server-Sent Events) | 새 값이 들어올 때마다 브라우저로 밀어 준다 |
| 실행 | 스크립트, Docker Compose, k8s(`deploy/k8s`) | 기본은 통합 실행 하나. 분리 실행은 Compose의 `split` 프로필 |

- **지금은 쓰지 않는 것**: 시계열 DB 서버(TimescaleDB, InfluxDB), 메시지 브로커(Kafka, MQTT 브로커). 저장 접근부와 전송부를 분리해 두어 필요할 때 교체한다.
- **Databricks 연계**: 팀 본 프로젝트가 Databricks에 올라가면 SQLite 데이터를 주기적으로 내보내 적재한다. 앞단 구조는 바꾸지 않는다.

## 3. 대상 공장: 다섯 개의 가상 공장

공장마다 구성안 파일 하나(`config/plant_<key>.yaml`)가 설비와 태그를 정의한다. 전체 설비·태그 목록은 `equipment-tags.md`에 있다.

| 키(경로) | plant_id | 공장 | 설비 · 태그 |
| --- | --- | --- | --- |
| `/rfcc/` | `PL-RFCC` | RFCC 회전기계 | 12 · 81 |
| `/cdu/` | `PL-CDU` | 상압증류 | 7 · 46 |
| `/hou/` | `PL-HOU` | 수첨분해 | 7 · 61 |
| `/rhds/` | `PL-RHDS` | 상압 잔사유 수첨탈황 | 6 · 43 |
| `/vrds/` | `PL-VRDS` | 감압 잔사유 수첨탈황 | 6 · 43 |

모든 데이터는 개발용 합성 값이다. 샘플 묶음에서 옮긴 마스터 값과 Nelson 기준 μ·σ가 구성안에 들어 있다. 설비는 유형 접두어로 구분한다: PM 펌프, FN 팬, CC 압축기, BL 송풍기, EX 팽창 터빈, ST 증기 터빈(공장마다 쓰는 유형은 구성안의 `equipment_types`).

### 3.1 계층

- **Plant · Unit Group · Unit**: 구성안의 `plant.unit_groups` 가 정의한다. 예: RHDS 는 `PL-RHDS` 아래 `UG-RHDS-01`(원료 공급)~`UG-RHDS-06`(생성물 분별) 여섯 계열이고, 계열마다 Unit 하나(`UNIT-RHDS-01`…)와 설비 하나가 있다.
- 설비는 `unit_id` 로 Unit에 속한다. 이 계층은 서버가 시작할 때 DB의 설비 마스터로 적재한다.

### 3.2 설비 ID 규칙

- **형식**: 공장 자료의 설비 번호를 그대로 쓴다. 보통 `{유형 접두어}-{번호}` 이고 같은 설비가 여럿이면 끝에 문자를 붙인다. 예: RHDS `P-3101`·`C-3101`·`F-3101`, CDU `P-8101A`, RFCC `C-1401`·`T-1401`.
- **유형 접두어**: 구성안 `equipment_types` 의 `prefix` (P 펌프, C 압축기, F 팬, B 송풍기, E 팽창 터빈, T 증기 터빈).
- **구동 관계**: 같은 축으로 직접 결합된 설비는 `driven_by` 로 적는다. RFCC 의 `B-1201` ← `E-1202`, `C-1401` ← `T-1401`. 피구동 설비의 `speed` 태그는 구동기의 속도를 그대로 쓰고, 구동기 이상은 피구동 설비 진동으로 번진다.
- **별칭**: `aliases` 필드를 지원하지만 현재 구성안은 쓰지 않는다.

### 3.3 태그 ID 규칙

- **복합키**: 태그는 `(equip_id, tag_id)` 로 식별한다. 태그명은 한 공장 안에서도 고유하게 둔다(RHDS·VRDS 는 자료에서 겹치던 `TI-B-3101` 을 설비 종류 문자를 넣어 `TI-B-P3101`·`TI-B-C3101`·`TI-B-F3101` 로 바꿨다).
- **형식**: `{신호 접두어}-…` 다. 접두어는 구성안 `signals` 의 `prefix` 이다(VI 진동, TI 온도, PI 압력, FI 유량, II 전류, SI 속도…). 뒷부분은 공장 자료의 이름을 따른다. 예: RFCC `VI-P-1101A`, CDU `VI-8101A`, RHDS `TI-B-P3101`.
- **단위**: 공장 사이에 통일하지 않고 태그마다 원본 자료의 단위를 쓴다.
- **공장 코드**: `rfcc` 같은 key 는 URL·DB 스키마 접두어, `PL-RFCC` 같은 plant_id 는 `plant` 테이블 키다. 이 ID 체계는 앞단에만 쓰며 MVP 의 설비 ID와 매핑하지 않는다.

### 3.4 ID를 나중에 바꾸기 쉽게 하는 원칙

- **한 곳에만 정의**: 설비 ID와 태그 ID는 `plant_<key>.yaml`에만 적는다. 3.2·3.3의 형식은 파일을 작성할 때의 약속이고, 코드는 ID 형식을 해석하거나 특정 ID를 하드코딩하지 않는다.
- **바꿀 때 고칠 곳**: 구성안 파일만 고치고, 그 공장의 DB를 비운 뒤 백필을 다시 돌린다.
- **시작 시 검증**: `driven_by`, `unit_id`, 신호·유형 참조에 적힌 ID가 구성안에 없거나, `(equip_id, tag_id)`·`seed_key` 가 겹치면 실행을 멈추고 어느 ID가 문제인지 알려 준다.
- **시드는 ID와 분리**: 태그별 시드는 태그 ID가 아니라 구성안의 `seed_key`(태그마다 고정된 값)로 만든다. ID 이름만 바꿔도 생성 값이 그대로 유지된다.
- **적용 범위**: 이 ID 체계는 앞단 데이터 생성기에만 적용한다. MVP와 테이블 정의 문서는 바꾸지 않는다.

## 4. 설정 파일

설정은 바뀌는 사람과 시점이 다른 세 종류로 나누고, 설비·태그·기준값 변경은 코드 수정 없이 파일만 고친다.

| 파일 | 내용 | 바뀌는 때 |
| --- | --- | --- |
| `config/plants.yaml` | 수집 서버가 여는 공장 목록: `key`(URL 접두어), 구성안·실행 파일, DB 파일. `plants.k8s.yaml` 은 DB 경로만 `/app/data/` 인 k8s 용 | 공장을 더하거나 뺄 때 |
| `config/plant_{rfcc,cdu,hou,rhds,vrds}.yaml` | 구성안: 계층, 설비 유형, 표준 태그, 설비, 태그(설명·단위·주기·자릿수·한계값(참고)·`control` μ·σ) | 공장 구성을 바꿀 때 |
| `config/runtime_*.yaml` | 실행 설정: 주기, 시간 모드, 백필 기간, 시드, 수집 서버 주소, 보관 기간, 지속 고장 설정 | 실행 방식을 바꿀 때 |

이상 사건은 별도 시나리오 파일 없이 생성기가 시드로 일정을 만들고, 지속 고장과 주입은 실행 중 API로 만든다(5장). 새 공장은 구성안과 실행 설정을 한 쌍 더하고 `plants.yaml` 에 한 줄 더한다.

### 4.1 구성안 설정 (`plant_<key>.yaml`)

```yaml
plant:
  plant_id: PL-RHDS
  name: "RHDS (상압 잔사유 수첨탈황)"
  config_version: 4
  unit_groups:
    - id: UG-RHDS-01
      name: "원료 공급"
      units:
        - { id: UNIT-RHDS-01, name: "잔사유 가압·공급" }
    # UG-RHDS-02 … 동일 형식

equipment_types:
  - { type: PM, name: 펌프, prefix: P, kind: pump }

signals:
  - { signal: vibration, prefix: VI, unit: "mm/s RMS", name: 케이싱 진동 속도 }

equipment:
  - equip_id: P-3101
    name: "고압 원료 공급 펌프 (High Pressure Feed Pump)"
    type: PM
    unit_id: UNIT-RHDS-01
    tags:
      - { tag_id: VI-A-P3101,
          signal: vibration,
          unit: "mm/s RMS",
          description: "구동측 수평 케이싱 진동",
          limits: { warn_hi: 4.5, alarm_hi: 7.1, trip_hi: 11 },   # 자료의 참고값
          interval_sec: 1,
          decimals: 3,
          seed_key: rhds-r001,    # 시드용 고정 키 (ID와 분리, 3.4)
          control: { mean: 1.65, sigma: 0.046 } }   # Nelson 기준 μ·σ (실제 파일은 전체 자릿수)
```

- **`plant`, `unit_groups`**: 시설 계층 테이블과 같은 내용이다. DB의 설비 마스터도 이 파일에서 만들어 넣어 시뮬레이터와 Skadi의 ID가 어긋나지 않게 한다.
- **`equipment`**: 설비 기본정보. `driven_by`(선택)는 구동 관계다. 별칭 `aliases` 도 쓸 수 있다.
- **`tags.control`**: Nelson 기준 μ·σ. 정상 신호 생성과 알람 판정이 모두 이 값을 쓴다. 신호의 모양은 이 값 하나로 정해지고 하루 주기·드리프트는 없다.
- **`tags.limits`**: 원본 자료의 경고·알람·정지 상·하한이다. 참고값으로 마스터(`/equipment`)에만 남기고 알람에는 쓰지 않는다. RFCC 는 `control` 의 μ ± 3σ 로 다시 계산했다(`limit_basis`). `normal_ref`·`limit_basis` 는 자료 원문 참고 문자열이다.
- **`tags.interval_sec`, `decimals`**: 태그별 측정 주기(온도 10초, 나머지 1초)와 값 반올림 자릿수. 판정도 반올림한 값으로 한다.

### 4.2 실행 설정 (`runtime_<key>.yaml`)

| 항목 | 기본값 | 설명 |
| --- | --- | --- |
| `units` | `[all]` | 이 프로세스가 맡을 Unit 목록. `all`이면 공장의 설비 전체를 한 프로세스에서 돌리고, `[UNIT-CDU-01]`처럼 주면 그 Unit만 돌린다 |
| `source_id` | `SIM-ALL` | 메시지의 `source_id`. 공장 파일은 `SIM-RFCC`, `SIM-CDU` 처럼 공장마다 다르고, Unit별로 나눌 때는 `SIM-CDU-1` 처럼 바꾼다 |
| `sample_interval_sec` | 10 | 값 생성 주기. 공장 파일은 1로 두고 온도 태그는 `interval_sec`(10)을 따른다 |
| `send_interval_sec` | 60 | 묶어서 보내는 주기(생성 주기의 배수). 공장 파일은 1 |
| `time_mode` | `live` | `backfill` / `live` / `accelerated` |
| `speed` | 60 | 가속 모드 배율 |
| `backfill_days` | 90 | 백필 기간. 공장 파일은 1 |
| `seed` | 20261108 | 같은 시드면 같은 데이터. 공장 파일은 공장마다 다른 값 |
| `collector_url` | `http://localhost:8000` | 수집 서버 주소. 공장 파일은 `http://localhost:8000/cdu` 처럼 공장 접두어까지 |
| `raw_retention_days` | 7 | 원시 값 보관 기간. 공장 파일은 1 |
| `persistent_fault_interval_min` | 15 | 지속 고장을 새로 여는 간격(분). 0 이면 끈다. 환경 변수 `PERSISTENT_FAULT_INTERVAL_MIN` 이 덮어쓴다 |
| `persistent_fault_max_open` | 3 | 공장당 동시에 열려 있는 지속 고장 상한(1 이상). 상한이면 그 회차는 건너뛴다. 환경 변수 `PERSISTENT_FAULT_MAX_OPEN` 이 덮어쓴다 |

- **시뮬레이터 단위**: 처음에는 `units: [all]`로 공장 전체를 한 프로세스로 돌린다. 코드는 Unit 단위로 설비를 묶어 돌리도록 만들어, 설정만 바꿔 Unit별 프로세스로 나눌 수 있게 한다.
- **나눠 실행할 때 지킬 것**: 값은 `(seed, 태그 seed_key, KST 날짜)`로 정해지므로 한 프로세스로 돌리든 Unit별로 나눠 돌리든 같은 값이 나온다. 구동 관계(`driven_by`)는 같은 Unit 안에서만 둔다.
- **통합 실행**: 수집 서버를 `--with-simulators` 로 띄우면 `plants.yaml` 의 다섯 공장 생성기가 같은 프로세스에서 돈다(`--time-mode`, `--backfill-days` 로 runtime 값을 덮어쓸 수 있다). 주입·멈춤·지속 고장·정답지는 통합 실행에서만 쓴다.

## 5. 신호 생성 모델

값은 매 순간 완전히 무작위가 아니라 이어지는 신호로 만들고, 그 위에 이상 사건을 얹는다. 정상 신호와 이상은 팀의 Nelson 판정 계약(data-v3)을 따른다. 자세한 설계는 `docs/superpowers/specs/2026-11-12-nelson-anomaly-design.md`.

### 5.1 정상 운전값

- **기준**: 태그마다 고정된 평균 μ와 표준편차 σ(`control`, data-v3 `baselines.json`). 하루 주기·드리프트·공정 부하는 없다.
- **생성**: z 를 순차로 만든다. `z = ρ·clip(z_prev, −2, 2) + √(1−ρ²)·N(0,1)`, ρ = 0.12(온도 0.25). 뽑은 z 로 Nelson 규칙 위반이 생기면 최대 100번 다시 뽑는다(억제 표집). 값 = `round(μ + σ·z, decimals)`.
- **목표**: 정상 구간은 일부러 깨끗하다. 설비별 위반 시간 비율 0.5% 미만.
- **상태**: 태그별 최근 14개 z. 백필에서 실시간으로 이어 가고, 재시작 때는 `ts_raw` 에서 읽어 복원한다.
- **재현성**: 시드는 (`runtime.seed`, 태그 `seed_key`, KST 날짜). 같은 시드로 다시 백필하면 같은 값이 나온다.
- `speed` 태그는 이상에서 바뀌지 않는다. `driven_by` 로 묶인 RFCC 짝은 같은 값을 낸다.

### 5.2 Nelson 8종 판정

시뮬레이터(상태 `status` 채움)와 수집 서버(`/alarms`)가 같은 구현(`common/nelson.py`)을 쓴다. z = (값 − μ) / σ, 판정은 보낸(반올림한) 값으로 한다.

| 규칙 | 조건 |
| --- | --- |
| 1 | \|z\| > 3 |
| 2 | 9점 연속 같은 쪽 |
| 3 | 6점 연속 엄격한 증가 또는 감소 |
| 4 | 14점 연속 증감 교대 |
| 5 | 3점 중 2점이 같은 쪽 \|z\| > 2 |
| 6 | 5점 중 4점이 같은 쪽 \|z\| > 1 |
| 7 | 15점 연속 \|z\| < 1 |
| 8 | 8점 모두 \|z\| > 1 이고 중심 양쪽에 모두 있음 |

경계와 같은 값은 '밖'이 아니고, 같은 값은 추세·교대를 끊는다. 조건이 성립한 마지막 표본에 표시하고 과거로 소급하지 않는다.

### 5.3 이상 사건

사건 = (설비, 규칙 1~8, 시작, 끝). 규칙별 z 템플릿(규칙 3 은 16 표본 톱니)을 설비의 대표 태그와 연관 태그(진동·온도·전류·압력·유량 등, 지연·방향·회복 시간 포함)에 덮어쓰고, 끝나면 태그마다 회복 시간 동안 0 쪽으로 되돌린다. 정답 분류는 기계/계측/제어 이상이다(정답지에만 있다).

| 종류 | 언제 | 끝 |
| --- | --- | --- |
| 자동 이상 | 설비·KST 날짜마다 하루 12건, 사건 시간 9.4%(합집합 ≈ 10%). 백필에도 생긴다 | 정해진 길이(200~1800초) 뒤 저절로 |
| 지속 고장 | 실시간 시작부터 `persistent_fault_interval_min`(기본 15분, `PERSISTENT_FAULT_INTERVAL_MIN`, 0 이면 끔)마다 놀고 있는 설비 하나에 무작위 규칙 | 멈춤 API 까지 |
| 주입 | `POST /{공장}/anomalies` | 멈춤 API 까지 |

지속 고장·주입이 살아 있는 설비에는 자동 이상이 끼지 않고, 새 지속 고장은 그 설비의 자동 이상을 끝낸다. 측정 신뢰도(품질)·결측·정비 정지·시나리오 파일·우연한 잡음 알람은 없다.

### 5.4 주입·멈춤·정답지 API (통합 실행 전용)

| 요청 | 동작 |
| --- | --- |
| `POST /{공장}/anomalies` `{equip_id, rule}` | `ceil(sim_now)+3`초 첫 표본부터 시작. 201 |
| `POST /{공장}/anomalies/{equip_id}/stop` | 그 설비의 살아 있는 사건을 `ceil(sim_now)+3`초에 끝내고 회복 |
| `POST /{공장}/anomalies/stop` | 공장의 살아 있는 사건 전부 멈춤. 끝낸 `event_id` 목록 |
| `GET /{공장}/anomalies` | 정답지: 출처·규칙·분류·원인·조치·반응 태그·시작·멈춤·끝 |

쓰기는 `/ingest` 와 같은 토큰 규칙이다. 분리 실행 409, 백필 중 503, 모르는 설비 404, 설비가 이미 주입·지속 고장 중이면 409, 규칙 번호 오류 422.

## 6. 메시지 형식 v2와 수집 서버 규칙

시뮬레이터는 묶음으로 `POST /ingest`를 보내고, 메시지에는 센서가 실제로 알 수 있는 값과 설비 상태만 담는다.

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

| 필드 | 필수 | 설명 |
| --- | --- | --- |
| `schema_version` | 예 | 메시지 형식 버전. 2.x 만 받는다(1.x 는 400) |
| `source_id` | 예 | 보낸 시뮬레이터(설비 쪽) |
| `batch_id` | 예 | 같은 묶음이 두 번 오면 한 번만 저장하는 기준 |
| `sent_at` | 예 | 보낸 시각 |
| `samples[].equip_id` | 예 | 설비 ID |
| `samples[].tag_id` | 예 | 태그 ID |
| `samples[].ts` | 예 | 측정 시각. 가속 모드에서는 시뮬레이터 시계 기준 |
| `samples[].value` | 예 | 측정값 (결측 없음) |
| `samples[].status` | 예 | 설비 상태 `Good` / `Bad`. 그 표본에서 Nelson 규칙이 하나라도 성립하면 `Bad` |

### 6.1 일부러 넣지 않는 것

- **단위, 신호 유형, 한계값, 기준 μ·σ**: 설비 마스터에 있다. 매번 실으면 크기가 커지고 마스터와 값이 어긋날 위험이 생긴다.
- **규칙 번호, 이상 사건 ID, 원인·분류**: 실제 센서는 정답을 알려 주지 않는다. 메시지와 `/alarms` 에 섞이면 Agent 평가에 정답이 새어 들어간다. 정답은 `GET /{공장}/anomalies` 에서만 낸다.

### 6.2 수집 서버 규칙

- 같은 `(equip_id, tag_id, ts)`는 한 번만 저장한다.
- 모르는 태그 ID나 형식이 틀린 행은 거절하고, 응답에 거절 건수와 이유를 돌려준다.
- 시각 순서가 뒤바뀐 행도 받는다. 집계는 측정 시각 기준으로 계산한다.
- 태그가 전송 주기 3번 동안 들어오지 않으면 그 구간을 `NoData`로 본다.
- 받은 묶음의 모든 표본을 Nelson 8종으로 다시 판정한다. 받은 `status` 와 다르면 경고 로그를 남기고 `/alarms` 는 수집 서버 판정을 따른다.

### 6.3 형식 변경 규칙

- **v2.x**: 필드 추가만 허용한다. 기존 수신 코드는 그대로 동작한다.
- **v3**: 필드 이름 변경·삭제나 타입 변경이다. 시뮬레이터, 수집 서버, DB, Skadi 조회부를 같이 고치고 DB를 비운 뒤 백필한다. (v1 → v2 에서 `quality` 가 `status` 로 바뀌었다.)

### 6.4 수집 서버 API

| 메서드 · 경로 | 용도 |
| --- | --- |
| `POST /{공장}/ingest` | 시뮬레이터 묶음 수신 |
| `GET /health`, `GET /{공장}/health` | 서버·공장 상태 (`sim_now`, `last_received_at`, `catching_up`, `live`) |
| `GET /{공장}/current` | 모든 태그의 현재값과 `status` (`Good` / `Bad` / `NoData`) |
| `GET /{공장}/equipment/{equip_id}/tags/{tag_id}/current` | 태그 현재값 |
| `GET /{공장}/timeseries?equip_id=&tag_id=&from=&to=&interval=1h` | 집계 시계열 |
| `GET /{공장}/alarms?equip_id=&rule=&from=&to=&open=` | Nelson 알람 목록. `from`/`to` 는 구간과 겹치는 알람, `open=true` 는 열린 것만. 심각도·원인은 없다 |
| `POST` · `GET /{공장}/anomalies…` | 이상 주입·멈춤·정답지 (5.4) |
| `GET /{공장}/stream` | SSE로 새 값 실시간 전달 |

정비·고장 이력 API(`/maintenance`, `/failures`)는 없앴다. 이력은 이 서버가 아니라 다른 서버가 판단한다.

## 7. 수집 서버의 DB 쓰기와 시간 모드

앞단이 직접 만드는 것은 센서 값과 설비 상태(`status`)뿐이다. 집계는 수집 서버의 배치가, Nelson 알람은 수집 서버가 값을 받을 때마다 만든다.

### 7.1 테이블별 채워지는 경로

| 경로 | 테이블 | 시점 |
| --- | --- | --- |
| 앞단 값이 바로 들어감 | `ts_raw` 원시 값 (보관 `raw_retention_days`), `tag_current` 현재값 | 수신 즉시 |
| 수신 때 판정 | `nelson_alarm` | 받을 때마다 |
| 배치가 가공 | `ts_10m`, `ts_1h` | 10분·매시 |
| 이상 장부 | `anomaly_event` (정답지 겸 생성기 상태) | 자동 이상 일정·주입·멈춤 때 |
| 구성안 파일에서 적재 | 시설 계층, 설비, 유형 매핑, 별칭, 표준 태그, 설비 태그(한계값은 참고값, `control_mean`·`control_sigma`) | 서버 시작 때 |
| 앞단과 무관 | 지식 문서·분할 본문, 대화 세션, 보고서, 평가 결과 | Skadi 쪽에서 |

테이블과 컬럼은 `collector/schema.sql`(스키마 v3)과 `table-plan.md` 를 따른다. 이전 DB 파일(v1·v2)은 열지 않고 알린다. 정비·고장 이력 테이블과 한계값 알람 테이블(`alarm_detail`)은 없앴다.

### 7.2 `ts_raw` 원시 값

| 컬럼 | 타입 | 설명 |
| --- | --- | --- |
| `equip_id`, `tag_id` | STRING | PK |
| `ts` | TIMESTAMP | PK, 측정 시각 |
| `value` | DOUBLE | 측정값 |
| `status` | STRING | Good / Bad |
| `batch_id` | STRING | 수신 묶음, 중복 확인용 |
| `received_at` | TIMESTAMP | 수신 시각 |

### 7.3 배치와 알람

- **10분 배치**: 최근 10분 원시 값으로 10분 집계를 만든다. 한계값 비교로 알람을 만들지 않는다.
- **매시 배치**: 1시간 집계(평균, 최소, 최대, 표준편차, 상태)를 만든다. 구간에 `Bad` 표본이 하나라도 있으면 `Bad`, 표본이 없으면 `NoData`.
- **매일 배치**: 보관 기간이 지난 원시 값을 지운다.
- **Nelson 알람 행**(`nelson_alarm`): `alarm_id`, `equip_id`, `tag_id`, `rule`, `direction`(hi/lo, 규칙 4·7·8 은 NULL), `occurred_at`, `cleared_at`, `last_hit_at`, `hit_count`, `value_at_occurrence`. (태그, 규칙)마다 연속 위반을 한 건으로 묶고, 15 표본 연속으로 그 규칙이 성립하지 않으면 닫는다. 심각도는 두지 않는다.
- 기존 한계값(경고·알람·정지) 알람과 심각도 규칙(Trip → High …)은 삭제했다.

### 7.4 시간 모드

- **백필 (`backfill`)**: 지난 며칠(기본 1일)을 빠르게 생성해 수집 서버로 보낸다. 실시간과 같은 경로를 타서 집계·판정 로직을 한 번에 검증한다. 백필에도 자동 이상은 생기지만, 주입·멈춤·지속 고장은 실시간에 들어간 뒤(`/health` 의 `live`)부터다.
- **실시간 (`live`)**: 백필이 끝난 시각부터 실제 시계로 이어서 생성한다.
- **가속 (`accelerated`)**: 시뮬레이터 시계를 `speed`배로 돌린다. 지속 고장에서는 실시간 구간으로 본다.
- **설계 원칙**: 모든 구성요소는 컴퓨터 시계가 아니라 ‘시뮬레이터 시계’를 본다. 배치도 측정 시각(`ts`) 기준으로 구간을 나눈다.

## 8. 골든셋 연계

평가의 정답은 수집 서버의 정답지(`GET /{공장}/anomalies`)이고, 정답은 데이터 흐름에 실리지 않는다.

- **원본**: 이상 사건 장부(`anomaly_event`)다. 사건마다 설비, 규칙(1~8), 분류(기계/계측/제어 이상), 원인, 조치, 반응 태그, 시작·멈춤 요청·끝 시각이 있다. 평가용 정답 세트는 이 정답지에서 뽑아 만든다.
- **사건의 출처**: 자동 이상(`spontaneous`, 시드로 정해져 백필에도 생긴다), 지속 고장(`persistent`, 멈출 때까지 이어진다), 주입(`injected`, 평가 담당이 API로 넣는다). 사례를 만들 때는 먼저 `POST /{공장}/anomalies/stop` 으로 공장을 비운 뒤 주입한다.
- **데이터는 섞는다**: 어느 사건이든 센서 값은 정상 값과 같은 경로로 들어간다. Agent는 어느 구간이 사건인지 알 수 없고, 알람(`/alarms`)만 본다.
- **정답은 숨긴다**: 평가 대상 모델에는 `/{공장}/anomalies` 를 절대 주지 않는다. 모델이 볼 수 있는 것은 `/alarms` 와 시계열뿐이다. 알람과 메시지에는 규칙 번호 외의 사건 ID·원인·분류가 없다.
- **정답과 알람은 다른 것**: 정답지는 생성기가 낸 사건이고 알람은 수집 서버가 값으로 다시 판정한 것이다. 사건 밖의 우연한 알람도 있을 수 있다(정상 신호는 억제 표집으로 거의 깨끗하다).
- **고르게 흩는다**: 평가 사례는 설비·규칙·분류가 겹치지 않게 주입으로 배치한다.
- **관리**: 정답지 조회와 주입·멈춤은 평가 담당만 쓰는 것으로 한다(조회는 서버가 막지 않는다). 쓰기는 `INGEST_TOKEN` 이 있으면 `X-Ingest-Token` 이 필요하다.

## 9. 폴더 구조와 개발 단계

Claude Code에서는 아래 구조로 저장소를 만들고, 단계별 완료 기준을 확인하며 순서대로 진행했다.

### 9.1 폴더 구조

```text
plant-simulator/
├─ config/
│  ├─ plants.yaml · plants.k8s.yaml     # 수집 서버가 여는 공장 목록
│  ├─ plant_{rfcc,cdu,hou,rhds,vrds}.yaml    # 구성안
│  └─ runtime_{rfcc,cdu,hou,rhds,vrds}.yaml  # 실행 설정
├─ common/
│  ├─ config_models.py           # Pydantic 설정 모델
│  ├─ message.py                 # 메시지 형식 v2
│  ├─ nelson.py                  # Nelson 8종 판정 (생성기·수집 서버 공용)
│  ├─ sim_clock.py               # 시뮬레이터 시계
│  └─ timefmt.py                 # 시각 형식 도우미
├─ simulator/
│  ├─ nelson_model.py            # 정상 신호(억제 표집)와 이상 덮어쓰기
│  ├─ anomaly.py                 # 이상 사건·규칙별 z 템플릿
│  ├─ schedule.py                # 자동 이상 일정(시드 고정)
│  ├─ sender.py                  # 전송 (HttpSender / LocalSender, 교체 가능)
│  └─ main.py                    # backfill / live / accelerated 실행
├─ collector/
│  ├─ app.py                     # FastAPI: /{공장}/ingest, 조회, /stream, 이상 API
│  ├─ plants.py · plant_context.py   # 공장 목록과 공장별 상태
│  ├─ ingest.py · alarms.py      # 수신 저장, Nelson 알람 행
│  ├─ anomaly_book.py            # 이상 사건 장부(정답지)
│  ├─ embedded.py                # 통합 실행: 공장별 생성기를 같은 프로세스에서 구동
│  ├─ storage.py · schema.sql    # SQLite 접근 (교체 가능), 스키마 v3
│  ├─ jobs.py                    # 집계·보관 배치
│  ├─ seed_master.py             # 구성안 → 설비 마스터 적재
│  ├─ settings.py · stream.py    # 환경 변수 설정, SSE
│  └─ static/index.html          # 디버깅 화면
├─ deploy/k8s/                   # 통합 실행 k8s 매니페스트
├─ docs/superpowers/             # 설계(specs)·계획(plans) 문서
├─ tests/
├─ data/{공장}.db                # 실행 때 생김 (git 밖)
├─ Dockerfile · docker-compose.yml
├─ README.md · table-plan.md · equipment-tags.md · log.md
└─ plant-simulator-plan.md
```

### 9.2 개발 단계

| 단계 | 내용 | 완료 기준 |
| --- | --- | --- |
| 1 | 최초 수첨탈황 구성: 설정 모델, 메시지 형식, 시뮬레이터 시계, 정상 신호, 수집 서버, 집계·알람 배치, 백필·가속 | 중복 묶음을 보내도 한 번만 저장되고, 같은 시드로 다시 돌리면 같은 값이 나온다 |
| 2 | RFCC 회전기계 구성안(설계 `2026-11-09-rfcc-simulator-design.md`) | 설비 12대·태그 81개가 설정 검증을 통과하고 현재값이 갱신된다 |
| 3 | 다중 공장: CDU·HOU·RHDS·VRDS 추가, `/{공장}/…` 경로, 공장별 DB (설계 `2026-11-09-multi-plant-design.md`) | 다섯 공장이 한 서버에서 같은 모양으로 열린다 |
| 4 | k8s 통합 실행: `--with-simulators`, `LocalSender`, 매니페스트 (설계 `2026-11-10-k8s-deploy-design.md`) | 파드 하나로 다섯 공장 생성·수집이 돈다 |
| 5 | Nelson 이상 생성·판정 (설계 `2026-11-12-nelson-anomaly-design.md`): 고정 μ·σ 정상 신호, 자동·지속·주입 이상, 메시지 v2 `status`, `nelson_alarm`, 주입·멈춤·정답지 API, DB 스키마 v3, 지속 고장 상한 | Nelson 판정이 생성기와 수집 서버에서 일치하고, 정상 구간의 위반 시간 비율이 설비별 0.5% 미만이다. 주입한 사건이 정답지와 알람에 나타난다 |

옛 한계값 알람·시나리오 파일·정비/고장 이력·메시지 품질(`quality`)은 5단계에서 없앴다. 진행 중 대신 정한 것과 남긴 것은 `docs/superpowers/plans/2026-11-12-nelson-anomaly-record.md`.

## 10. 미결 사항과 참고 문서

현재 남은 항목은 아래와 같다. 확정된 것(공장 다섯 곳, 통합·분리 실행, HTTP 전송, ID 체계)은 위 장에 적었다.

- [ ] k8s 자원(CPU 500m)에서 백필 시간을 다시 재지 않았다. README의 "약 15분"은 옛 생성기 기준이다.
- [ ] 자동 이상이 지속 고장·주입이 열리는 창 바로 앞에서 시작한 경우, 정답지의 끝 시각이 잘린 것으로 표시되지 않는다(드묾).
- [ ] 평가용 정답 세트의 관리 담당과 사례 구성(설비·규칙 분포)을 정한다.
- 참고 문서: `README.md`(실행·API), `table-plan.md`(테이블), `equipment-tags.md`(공장별 설비·태그), `docs/superpowers/specs/`(설계).
