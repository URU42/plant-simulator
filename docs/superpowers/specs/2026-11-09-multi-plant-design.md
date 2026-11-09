# 다중 공장 시뮬레이터 설계 (CDU · HOU · RHDS · VRDS 추가, 공장별 경로)

작성일 2026-11-09. 앞선 두 문서(기획서 `plant-simulator-plan.md`, RFCC 설계 `2026-11-09-rfcc-simulator-design.md`)의 확장.
세 샘플 묶음 `cdu-sample/`, `hou-plant-sample/`, `RHDS_VRDS_v3/`(RHDS·VRDS 두 공장)을 구성안으로 옮기고,
수집 서버 하나가 여섯 공장(hds, rfcc, cdu, hou, rhds, vrds)을 `/{공장}/…` 아래에 같은 모양으로 서빙한다.

## 1. 의도

- 네 공장의 값이 RFCC 처럼 끝없이 새로 만들어진다. 엔진은 RFCC 신호 모델 하나를 쓴다. 각 묶음 생성기의 세부 거동(탱크 전환, 부하 변경 등)은 재현 대상이 아니다.
- 각 공장의 **마스터 값은 그대로 유지한다**: 계층 ID·이름, 설비 ID·이름, 태그명, 설명, 단위, 주기, 정상 기준, 경고/알람/정지 상·하한. 출처는 각 Excel 의 `설비 설명`·`설비 태그` 시트다.
- 조회는 `/cdu/current`, `/hou/`, `/rhds/health` 처럼 공장 접두어로 한다. 기존 RFCC·HDS 도 `/rfcc/…`, `/hds/…` 로 옮긴다.
- 데이터 키는 자료의 계약대로 **(설비 ID, 태그명, 측정 시각)** 복합키다. RHDS/VRDS 는 태그명이 설비 사이에서 겹친다(`TI-B-3101` 이 P-3101·C-3101·F-3101 에 모두 있음).

## 2. 목표 / 비목표

목표
- `config/plant_{cdu,hou,rhds,vrds}.yaml` + 실행·시나리오 설정. 수치는 Excel 마스터 표에서 기계적으로 옮긴다.
- 한계값에 하한을 지원하고 알람 판정에 쓴다(CDU 36칸, HOU 38칸, RHDS/VRDS 17칸이 하한).
- 품질에 `Uncertain`(값 있음)을 추가한다(HOU).
- 수집 서버: 공장 목록 파일 하나로 여러 공장을 띄우고, 공장마다 DB 파일·배치·화면을 따로 가진다.
- Docker Compose: 수집 서버 1 + 공장별 시뮬레이터 6.

비목표
- Excel 재생, 각 묶음 생성기의 식 이식, 운전 변경 이벤트(CDU 탱크 전환, HOU 부하 증가) 재현.
- 공장 간 조인·통합 조회, 인증, 차트.
- 파형 데이터(RHDS/VRDS 의 삭제된 파형 시트).

안티 목표
- 어떤 공장의 태그명·단위·한계값이 자료와 다르다.
- 접두어를 바꿨는데 공장마다 API 모양이 다르다.
- 1초 전송이 여섯 공장에서 밀린다.

## 3. 제약

- 기존 의존성만 쓴다. 구성안 생성 스크립트(일회용)에서만 `openpyxl` 을 쓰고 커밋하지 않는다.
- 코드는 ID 형식을 해석하지 않는다. 공장 접두어(`cdu` 등)는 공장 목록 파일의 키다.
- 값은 `(seed, seed_key, 측정 시각)` 의 순수 함수.
- 샘플 폴더 세 개는 `.gitignore` 에 넣는다(합 1 GB).

## 4. 결정 사항

### 4.1 복합키 (설비 ID, 태그명)

| 곳 | 변경 |
| --- | --- |
| 구성안 | `tag_id` 는 **같은 설비 안에서만** 고유. `PlantConfig` 의 조회 도우미는 `(equip_id, tag_id)` 키를 쓴다: `tag_by_key()`, `tag_interval_sec(equip_id, tag_id, default)`, `resolve(ref, equip_hint=None) -> TagKey`. `TagKey = tuple[str, str]` |
| 태그 참조 문자열 `TagRef` | `"EQUIP/TAG"`(예: `P-3101/TI-B-3101`). 평범한 `"TAG"` 도 허용하되 공장 안에서 고유할 때만 풀리고, 겹치면 `ConfigError` 가 후보 설비를 알려 준다. `correlations.source/target`, 시나리오 `related[].tag_id` 가 이 형식. 기존 HDS·RFCC 파일은 그대로 읽힌다 |
| 시나리오 이벤트 | `equip_id` 필수, `tag_id` 선택(없으면 설비의 모든 태그). `related[].tag_id` 는 `TagRef`(평범한 태그명이면 이벤트의 `equip_id` 안에서 먼저 찾는다). 기존 `tag_id` 만 있는 이벤트는 고유 태그명이면 설비를 자동으로 채운다 |
| 메시지 v1 | 변경 없음(이미 `equip_id`, `tag_id` 둘 다 있음). 수집 서버는 `(equip_id, tag_id)` 가 마스터에 있는지 본다 |
| DB | `equipment_tag` PK `(equip_id, tag_id)`, `ts_raw` PK `(equip_id, tag_id, ts)`, `tag_current` PK `(equip_id, tag_id)`, `ts_10m`·`ts_1h` PK `(equip_id, tag_id, bucket_start)`. 집계·알람 GROUP BY 도 두 컬럼 |
| 조회 API (공장 접두어 아래) | `GET /{p}/equipment/{equip_id}/tags/{tag_id}/current`, `GET /{p}/timeseries?equip_id=&tag_id=&from=&to=&interval=`. `/tags/{tag_id}/current` 는 없앤다 |
| 신호 모델·생성기·시나리오 내부 | 태그 사전·캐시·시드 키를 모두 `TagKey` 로. 시드는 그대로 `seed_key` 에서(태그 `seed_key` 는 공장 안에서 고유) |
| 예전 DB 파일 | PK 는 `ALTER` 로 못 바꾼다. `schema_meta(version)` 테이블을 두고(현재 2), 버전이 없거나 낮은 DB 를 열면 `ConfigError("DB … was created by schema v1; move or delete it, or pass another --db")` 로 멈춘다. PR #3 의 컬럼 추가 마이그레이션은 이 검사로 대체한다(새 DB 는 항상 새 스키마). HDS `data/plant.db` 도 다시 만든다(90일 백필 약 8분) |

### 4.2 한계값 상·하한

```yaml
limits: { warn_hi: 3.0, alarm_hi: 4.5, trip_hi: 7.1 }                 # 기존 그대로
limits: { warn_lo: 2.02, alarm_lo: 1.62 }                            # 하한만
limits: { alarm_lo: 115, alarm_hi: 140 }                             # 양쪽, 경고 없음
```

- `Limits` 의 여섯 필드가 모두 선택. 최소 하나는 있어야 한다. 있는 것끼리 순서 검증: `warn_hi < alarm_hi < trip_hi`, `trip_lo < alarm_lo < warn_lo`, 그리고 모든 lo < 모든 hi.
- 알람 배치: 상한은 10분 최대값 `>` 한계, 하한은 10분 최소값 `<` 한계. 심각도 매핑은 그대로(Soft/Alarm/Trip → Low/Medium/High). `alarm_detail` 에 `direction TEXT NOT NULL`('hi' / 'lo') 컬럼 추가, 열린 알람 키는 `(equip_id, tag_id, limit_type, direction)`. `exceed_pct` 는 하한일 때 `(limit − actual) / |limit| × 100`.
- `equipment_tag` 에 `warn_lo, alarm_lo, trip_lo REAL` 추가.
- HDS 신호 모델의 "정상값을 warn_hi 아래에" 와 우연 잡음 알람의 크기(`warn_hi − base`)는 `warn_hi` 가 있을 때만 쓴다. 없으면 우연 잡음 알람은 건너뛴다(RFCC 때 규칙을 "limits 없음"에서 "warn_hi 없음"으로 넓힘).

### 4.3 품질

`Quality = Literal["Good", "Uncertain", "Bad", "ScanOff"]`. `Uncertain` 은 값이 있어야 한다. 집계의 `AVG/MIN/MAX` 는 값이 있는 행(Good·Uncertain)으로, `bad` 수는 `quality NOT IN ('Good','Uncertain')`.
자료의 `Scan Off` 는 기획서 표기 `ScanOff` 로 적는다(화면·DB 모두). **내 판단**: 기획서 표기를 유지한다.

시나리오에 **우연한 품질 저하** `random_quality` 를 추가한다(HOU 생성기 규칙의 일반화, 모든 공장에서 선택 사용):

```yaml
random_quality:
  uncertain_rate: 0.00003            # 샘플당 Uncertain 확률 (10초 태그는 ×10)
  bad_segments_per_tag_week: 1.0     # 태그당 주당 평균 Bad 구간 수 (poisson)
  bad_duration: [30s, 400s]          # Bad 구간 길이 (균등)
```

(태그, 날짜) 로 시드해 순수 함수로 만든다. 값 패턴·`quality` 이벤트와 같은 자리에서 `scenario.apply` 가 얹는다.

### 4.4 신호 모델 확장 (`simulator/rfcc_model.py`)

- 종류 추가: `differential_pressure`(차압, 식 `b·(1 + 0.2·local) + z`), `process_temp`(공정 유체 온도, `bearing_temp` 와 같은 식). 정지값은 차압 0, 공정 온도 25 °C.
- `AIR_SHAFT_KINDS` 에 `fan` 추가(속도 계수 0.12).
- 그 밖의 식·구조는 그대로. 공장마다 모델 객체가 따로 있으므로 공통 부하·외기는 공장 안에서만 공유된다.

`측정 구분`·단위·설명 → `signal` 대응(구성안 생성 규칙):

| 측정 구분 | 조건 | signal |
| --- | --- | --- |
| 진동 | 단위 `um pk-pk` | `shaft_vibration` |
| 진동 | 그 외 | `vibration` |
| 온도 | 설명에 `윤활`·`오일` | `oil_temp` |
| 온도 | 설명에 `유체`·`인출` | `process_temp` (흡입·토출 표현보다 우선. 예: "흡입 유체 온도") |
| 온도 | 설명에 `토출` | `discharge_temp` |
| 온도 | 설명에 `흡입` 이고 `베어링` 아님 | `suction_temp` |
| 온도 | 그 외(베어링·패드·메인) | `bearing_temp` |
| 압력 | 설명에 `윤활` | `oil_pressure` |
| 압력 | 설명에 `흡입`·`입구` | `suction_pressure` |
| 압력 | 그 외(토출·출구) | `discharge_pressure` |
| 차압 | | `differential_pressure` |
| 유량 | | `flow` |
| 전류 | | `current` |
| 회전속도 | | `speed` |
| 축방향 위치 | | `axial_position` |

설비 유형 `kind`: Pump → pump, Compressor → compressor, Fan → fan(`equipment_types`: PM/CC/FN). 구동기 설비가 자료에 없으므로 `driven_by` 는 쓰지 않는다.

### 4.5 구성안 4개 생성 규칙 (일회용 스크립트, 결과 yaml 만 커밋)

| 항목 | 출처 |
| --- | --- |
| `plant.plant_id`, `name` | `설비 설명` 시트 계층의 Plant 행 (`PL-CDU` / `PL-HOU` / `PL-RHDS` / `PL-VRDS`) |
| `unit_groups[].units[]` ID·이름 | 같은 시트의 UnitGroup·Unit 행. 설비가 나오는 순서대로 |
| `equipment[]` | 설비 ID·이름(국문 (영문)), `type`(유형: Pump→PM, Compressor→CC(왕복동 포함), Fan→FN), `unit_id`, `seed_key`, `aliases: []`. 순서는 Unit ID 번호순. Unit 이름은 시트 그대로(CDU 는 `U-811` 같은 코드) |
| 태그 `tag_id, description, unit, limits` | `설비 태그` 시트의 태그명·태그 설명·공학 단위·경고/알람/정지 하한·상한. 빈 칸은 쓰지 않는다 |
| 태그 `normal_ref`, `limit_basis` (새 선택 필드, 문자열) | 같은 시트의 `정상 기준(참고)`(CDU 는 `"1.4~2.8"` 같은 범위, HOU 는 숫자, RHDS/VRDS 는 없음)과 `한계값 근거`. 마스터 표를 그대로 남기기 위한 것이며 코드는 해석하지 않는다. `equipment_tag` 에 `normal_ref TEXT, limit_basis TEXT` 컬럼을 두고 `/equipment` 응답에도 넣는다. **내 판단**: 스키마를 v2 로 고정하는 지금 함께 넣는다 |
| `interval_sec` | 데이터 시트의 연속 두 시각 차 (온도 10, 나머지 1) |
| `normal.base` | 데이터 첫 6시간의 Good·Uncertain 값 평균. 이상 이벤트는 모두 6시간 뒤에 시작한다(가장 이른 것이 CDU 16.7시간). HOU 의 "정상 기준" 단일값·CDU 의 범위 대신 데이터 평균을 쓴다. **내 판단** |
| `normal.noise_sd` | 첫 1시간 연속 차분의 표준편차를 종류별 상수로 나눈 값: 기본(τ 6 s + 흰 잡음 0.12) 0.58, 온도(τ 80 s, 10 s 격자) 0.49, 속도(τ 5 s) 0.60, 축 위치(τ 12 s) 0.40. **내 판단** |
| `decimals` | 첫 1시간 값의 소수 자릿수 최대 |
| `seed`(runtime) | 각 생성기의 시드: CDU 20261101, HOU 20261109, RHDS 20261110, **VRDS 20261111**. 원 자료는 두 공장을 "공통 부하 8200+Plant번호"로 구분했다. 같은 시드를 쓰면 구조가 같은 두 공장이 기준값만 다른 쌍둥이 파형이 되므로 VRDS 는 시드를 하나 올린다. **내 판단** |
| `seed_key` | 공장 접두어를 붙인다: 설비 `cdu-e01…`, 태그 `cdu-r001…`. 공장이 달라도 같은 시드를 쓸 때 잡음이 겹치지 않게 한다. RFCC·HDS 기존 키는 그대로 |
| `plant.signal_model` | `rfcc` |

`runtime_{cdu,hou,rhds,vrds}.yaml` 은 `runtime_rfcc.yaml` 과 같고 `source_id`(`SIM-CDU` 등)·`seed`·`collector_url`(`http://localhost:8000/cdu` 처럼 공장 접두어 포함)만 다르다. `runtime_rfcc.yaml`·`runtime.yaml` 의 `collector_url` 도 `/rfcc`, `/hds` 를 붙인다.

### 4.6 시나리오 4개 (매주 반복, `swell` / `quality`)

출처는 각 묶음의 `평가전용/`. 크기는 자료 값(CDU·HOU)을 따르고, 자료에 없는 RHDS/VRDS 는 아래 **내 판단** 값. 운전 변경 이벤트는 넣지 않는다.

| 파일 | 이벤트 | 대상 · 시작(KST) · 길이 | 크기 |
| --- | --- | --- | --- |
| `scenario_cdu.yaml` | EV-CDU-001 NDE 베어링 열화 | P-8303A, 11-02 12:06:40, 47h | `VI-8303B` +1.85(×2.25 → +1.25·base 1.47), related `VI-8303A` 0.17(+0.18·1.74 / 1.85), `TI-8303A` 2.97(+5.5 / 1.85) |
| | EV-CDU-002 흡입 스트레이너 막힘 | P-8101A, 11-01 16:40, 66h 40m | `PI-8101A` −0.42, related `VI-8101A` −0.39(+0.16 / −0.42) |
| | EV-CDU-003 RTD 단선 | F-8201 `TI-8201A`, 11-03 11:30, 25m | `quality: Bad` |
| `scenario_hou.yaml` | EV-HOU-001 환류 펌프 캐비테이션 징후 | P-2601, 11-02 21:00(45h), 38h | `VI-2601A` +3.0(알람 4.5 초과, 정지 7.1 미만), related `PI-2601A` ratio −0.667(편차 −2.0 → 약 2.3: 알람 하한 2.5 미만·정지 하한 1.5 위), `FI-2601` ratio −0.1·base/3.0 |
| | EV-HOU-002 FD/ID 팬 NDE 베어링 | FN-2401, 11-01 20:00(20h), 63h | `TI-2401B` +28(→ 약 81 °C), related `VI-2401B` +1.4(→ 약 3.3) |
| | EV-HOU-003 Scan Off | P-2501 `FI-2501`, 11-03 22:00(70h), 30m | `quality: ScanOff` |
| | `random_quality` | 전 태그 | HOU 생성기 값(위 4.3) |
| `scenario_rhds.yaml` | EV-RHDS-001 흡입 조건 악화 | P-3101, 11-01 17:46:40(64000s), 40m | `VI-A-3101` +1.5, related `PI-S-3101` −1.0, `PI-D-3101` −3.0, `FI-3101` −20 |
| | EV-RHDS-002 Scan Off | P-3103 `FI-3103`, 11-02 01:00(90000s), 120s | `quality: ScanOff` |
| `scenario_vrds.yaml` | EV-VRDS-001 윤활유 냉각 저하 | C-4101, 11-03 12:00(216000s), 2h 15m | `TI-LO-4101` +8, related `TI-B-4101` +6 |
| | EV-VRDS-002 Bad | F-4101 `VI-A-4101`, 11-02 23:30(171000s), 60s | `quality: Bad` |

`related.ratio` 는 기준 태그 편차(magnitude)에 대한 배율이다. HOU 의 크기는 "알람 수준·정지 미도달" 서술에 맞춘 값이며 자료에 숫자가 없는 것은 **내 판단**이다. RFCC 와 마찬가지로 `failure_id` 는 주지 않는다.

### 4.7 수집 서버: 공장 목록과 접두어

`config/plants.yaml` (새 파일, 수집 서버의 유일한 설정 입력):

```yaml
plants:
  - { key: hds,  plant: config/plant_hds.yaml,  runtime: config/runtime.yaml,      scenario: config/scenario_dev.yaml,  db: data/hds.db }
  - { key: rfcc, plant: config/plant_rfcc.yaml, runtime: config/runtime_rfcc.yaml, scenario: config/scenario_rfcc.yaml, db: data/rfcc.db }
  - { key: cdu,  plant: config/plant_cdu.yaml,  runtime: config/runtime_cdu.yaml,  scenario: config/scenario_cdu.yaml,  db: data/cdu.db }
  - { key: hou,  ... }
  - { key: rhds, ... }
  - { key: vrds, ... }
```

- `key` 는 URL 접두어(소문자·숫자·`-`), 고유. `scenario` 는 선택.
- 서버 구조: 라우트는 한 번만 정의하고 경로 앞에 `/{plant_key}` 를 둔다. 의존성이 `plant_key` 로 `PlantContext`(plant, runtime, storage, jobs, broadcaster, catch_up_task, equipment_listing)를 찾고 모르면 404. lifespan 에서 공장마다 `PlantContext` 를 만든다. 기존 `create_app(plant_path, runtime_path, db_path, scenario_path)` 는 공장 하나짜리 목록으로 감싸는 호환 함수로 남긴다(테스트용, `key` 는 `plant`).
- 공장 접두어 아래 경로: `POST /ingest`, `GET /health`, `GET /equipment`, `GET /current`, `GET /equipment/{equip_id}/tags/{tag_id}/current`, `GET /timeseries`, `GET /alarms`, `GET /stream`, `GET /`(화면). `GET /{p}` 는 `/{p}/` 로 리다이렉트.
- 루트: `GET /` 는 공장 목록 페이지(키·plant_id·이름·설비 수·태그 수·링크). `GET /health` 는 `{"status": "ok", "plants": {key: {plant_id, sim_now, last_received_at, catching_up}}}`. compose healthcheck 는 루트 `/health`.
- 화면 `index.html` 은 상대 경로(`equipment`, `current`, `stream`)를 쓰고 제목에 공장 이름을 넣는다. 한 파일을 모든 공장이 쓴다.
- CLI: `python -m collector.app --plants config/plants.yaml --host --port`. 기존 `--plant/--runtime/--scenario/--db` 인자는 없앤다(`PLANTS_CONFIG` 환경 변수).
- 공장별 DB·배치·보관은 각자의 runtime 을 따른다. 배치 따라잡기 태스크도 공장별.

### 4.8 시뮬레이터·실행

- 시뮬레이터는 변경 없음. 공장마다 프로세스 하나: `--plant config/plant_cdu.yaml --runtime config/runtime_cdu.yaml --scenario config/scenario_cdu.yaml --collector-url http://collector:8000/cdu`.
- compose: `collector` 1 + `sim-hds, sim-rfcc, sim-cdu, sim-hou, sim-rhds, sim-vrds`. 모두 `--time-mode ${SIM_TIME_MODE:-backfill}`. 공장별 시작 순서는 `depends_on: collector healthy`.
- 용량: 1초 태그 합계 약 320개(hds 제외) → 하루 보관 시 원시값 합계 약 4 GB. HDS 는 90일 백필 8분, DB 수백 MB. README 에 적는다.
- 수집 서버는 프로세스 하나다. 여섯 공장이 동시에 첫 백필을 하면 SQLite 쓰기가 사실상 직렬화돼 전체 백필이 수 분 걸리고, 먼저 실시간에 들어간 공장의 1초 전송은 재시도로 버틴다. 정상 상태(초당 약 6 요청)에는 여유가 있다. 수용하고 README 에 적는다.
- `.gitignore`: `cdu-sample/`, `hou-plant-sample/`, `RHDS_VRDS_v3/`.

### 4.9 문서

README: 여섯 공장 표(키·설비·태그·주기·근거 폴더), 공장 목록 파일, 접두어 API 표, 복합키·하한 한계값·`Uncertain`·`random_quality`·`TagRef`, 예전 DB 파일 안내, 용량.

## 5. 검증

1. 네 구성안이 읽히고 설비·태그 수가 자료와 같다(CDU 7·46, HOU 7·61, RHDS 6·43, VRDS 6·43). RHDS 에서 `TI-B-3101` 이 세 설비에 있다. 한계값 표본 몇 개가 Excel 값과 같다(예: CDU `PI-8101A` warn_lo 2.02 / alarm_lo 1.62, HOU `ZI-2301` 다섯 칸, RHDS `SI-3101` 8500/10000).
2. 복합키: 두 설비의 같은 태그명이 서로 다른 값으로 생성·저장·조회된다. 평범한 태그명 참조가 겹치면 후보 설비를 알려 주는 `ConfigError`.
3. 하한 알람: 10분 최소값이 `warn_lo` 아래면 `direction: lo`, Low 알람; 올라오면 해제. 상한 알람은 기존 테스트 그대로.
4. `Uncertain` 샘플이 저장되고 집계 평균에 들어가며 `bad` 수에는 안 들어간다. `random_quality` 가 결정적이고 비율이 설정 근처.
5. 공장 두 개를 `plants.yaml` 로 띄운 수집 서버: `/a/ingest` 로 보낸 값이 `/b/current` 에 없다. `/health` 루트가 두 공장을 보여 준다. `/a` → `/a/` 리다이렉트. 모르는 키 404.
6. 예전 스키마(v1) DB 파일을 열면 명확한 `ConfigError`.
7. 기존 테스트 전부(복합키 API 경로 변경에 맞춰 고친 것 포함) 통과.
8. 실제 실행: compose 로 여섯 공장을 띄워 `/`, `/cdu/`, `/rhds/` 에서 값이 1초마다 바뀌고, `/health` 에 여섯 공장이 보인다. 백필 후 `data/` 크기를 README 에 적는다.

## 6. 작업자에게 맡기는 것

- `TagKey` 를 코드 전반에 어떻게 전달할지(튜플 vs 작은 dataclass). 테스트가 기대하는 공개 이름은 4.1 표의 것.
- 구성안 생성 스크립트의 위치(스크래치)와 형태. 결과 yaml 의 머리 주석에 출처·생성 규칙(4.5)·"한계값은 자료 그대로"를 적는다.
- `PlantContext` 의 자료구조와 라우터 분할(파일을 `collector/app.py` 와 `collector/plant_context.py` 로 나누기를 권한다).
- 공장 목록 페이지의 모양. 링크와 수만 보이면 된다.
