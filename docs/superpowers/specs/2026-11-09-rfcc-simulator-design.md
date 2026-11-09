# RFCC 구성안 시뮬레이터 설계 (기획서 확장)

작성일 2026-11-09. 기획서 `plant-simulator-plan.md` 는 수첨탈황 구성안(plant_hds)을 대상으로 썼다.
이 문서는 `rfcc-20261109/` 샘플 자료를 기준으로 RFCC 구성안을 추가하고, 1초 주기 데이터를 끝없이 만들어
디버깅용 화면에서 보는 데 필요한 변경을 정한다. 기획서에 없는 내용은 이 문서가 근거다.

## 1. 의도

- Skadi 앞단 데이터 생성기가 RFCC 샘플 자료와 같은 모양의 값을 **끝없이** 만든다. Excel 재생이 아니다.
- 샘플 자료의 생성기(`rfcc-20261109/support/generate.py`, `config.json`)가 정한 설비 12대·태그 81개·기준값·잡음·연동 관계를 그대로 옮긴다. 수치를 새로 정하지 않는다.
- 온도 16개 태그는 10초, 나머지 65개 태그는 1초 주기다.
- 원시값은 하루 전까지 되돌아볼 수 있어야 한다. 그 이전 행은 몇 분마다 지운다.
- 수집 서버가 서빙하는 페이지 하나에서 81개 태그의 값이 1초마다 갱신되는 것을 본다. 디버깅용이라 값만 보이면 된다.

## 2. 목표 / 비목표

목표
- `config/plant_rfcc.yaml` 하나로 RFCC 구성안을 돌린다. 기존 plant_hds 구성안과 코드·설정 형식을 공유한다.
- 모든 값은 지금처럼 `(seed, seed_key, 측정 시각)` 의 순수 함수다. 백필→실시간 연속, 재시작 연속, 재현 가능.
- 샘플 자료의 두 이상 징후(흡입 조건 악화, 베어링·윤활 점검)와 센서 품질 Bad 구간을 시나리오로 옮기고 주기적으로 반복시킨다.
- Docker Compose 기본 구성을 RFCC 로 바꾼다. 환경 변수로 plant_hds 로 되돌릴 수 있다.

비목표
- Excel 파일을 읽거나 재생하지 않는다. `rfcc-20261109/` 는 참고 자료이며 git 에 넣지 않는다(.gitignore).
- 알람 한계값을 만들지 않는다. 자료가 "정상 중심값을 경보값으로 바꾸지 않는다"고 못 박았다. RFCC 태그는 한계값이 없고 알람 배치는 그 태그를 건너뛴다.
- 차트, 로그인, 필터 등 화면 기능. 표에 값·품질·시각만 보인다.
- 샘플 자료의 통계(평균·표준편차·자기상관)를 수치로 재현한다고 검증하지 않는다. 모델 식을 옮기는 것이 목표다.

안티 목표(이러면 실패)
- 자정이나 재시작에서 값이 튀어 보인다.
- 1초 전송이 밀려 화면이 멈춘다.
- plant_hds 구성안의 기존 동작·테스트가 깨진다.

## 3. 제약

- Python 3.11, 기존 의존성만 쓴다(NumPy, Pydantic, FastAPI). scipy·pandas 를 추가하지 않는다.
- 코드는 ID 형식을 해석하지 않는다. 설비·태그 ID 는 yaml 에만 있다.
- 신호 종류 이름(`signal`)은 코드가 식을 고르는 키다. ID 가 아니라 유형이므로 코드에 적어도 된다.
- 기존 메시지 형식 v1, `/ingest` 규칙, 배치 구조(측정 시각 기준)는 바꾸지 않는다.

## 4. 결정 사항

### 4.1 구성안 파일 형식 확장 (`common/config_models.py`)

| 항목 | 변경 | 이유 |
| --- | --- | --- |
| `plant.signal_model` | 새 필드. `hds`(기본) / `rfcc` | 어느 신호 모델로 값을 만들지 구성안이 정한다 |
| `TagConfig.limits` | 선택(`None` 허용) | RFCC 는 한계값이 없다 |
| `TagConfig.delay_samples` | 기본값 1 | limits 가 없으면 쓰지 않는다 |
| `TagConfig.location` | 선택 | RFCC 는 위치 코드 대신 설명이 있다 |
| `TagConfig.description` | 새 선택 필드 | 샘플 자료의 태그 설명. 화면과 `equipment_tag` 에 남긴다 |
| `TagConfig.interval_sec` | 새 선택 필드. 없으면 `runtime.sample_interval_sec` | 온도 10초, 나머지 1초 |
| `TagConfig.decimals` | 새 필드. 기본 3 | 축방향 위치(0.04 mm, 잡음 0.0008)는 5자리가 필요하다. 자료의 반올림 규칙(ZI 5, 나머지 4) |
| `NormalModel.daily_amp`, `drift_sd` | 기본 0 | rfcc 모델은 `base`, `noise_sd` 만 쓴다 |
| `EquipmentConfig.grade` | 선택 | 자료에 등급이 없다 |
| `EquipmentConfig.seed_key` | 새 선택 필드. `signal_model: rfcc` 면 필수 | 설비 단위 느린 성분(`local_e`)의 시드. 설비 ID 를 바꿔도 값이 유지된다(기획서 3.4 원칙을 설비 수준까지) |

검증 규칙 추가
- `interval_sec` 는 `sample_interval_sec` 의 배수이고 86400 을 나눠야 한다. `send_interval_sec` 의 배수일 필요는 없다(10초 태그를 1초마다 보내는 묶음에 섞는다).
- `signal_model: rfcc` 일 때 각 태그의 `signal` 은 rfcc 모델이 아는 종류여야 한다(아래 4.2 표). 모르면 시작을 멈추고 태그 ID 와 종류를 알려 준다.
- `driven_by` 로 이어진 두 설비가 둘 다 `speed` 태그를 가지면 기준값이 같아야 한다(같은 축).

`equipment_tag` 테이블: `location`, `warn_hi`, `alarm_hi`, `trip_hi` 를 NULL 허용으로, `description` 컬럼 추가. `equipment.grade` NULL 허용.
스키마는 `CREATE TABLE IF NOT EXISTS` 라 기존 DB 파일에는 적용되지 않는다. RFCC 는 새 DB 파일(`data/rfcc.db`)을 쓴다.

### 4.2 RFCC 신호 모델 (`simulator/rfcc_model.py`, 새 파일)

`SignalModel` 과 같은 인터페이스(`values`, `normal_values`, `stopped_values`)를 가진 `RfccSignalModel`.
`SampleGenerator` 가 `plant.signal_model` 을 보고 둘 중 하나를 만든다. 시나리오·연관 규칙·정비 처리(`scenario.py`)는 모델을 가리지 않는다.

**공통 성분** (샘플 생성기의 `LOAD`, `AMBIENT`, `SPEEDS` 에 해당)

| 이름 | 식 | 비고 |
| --- | --- | --- |
| 공정 부하 `L(t)` | `0.025·sin(2πt/110000) + OU(sd .004, τ 1400 s) + 부하 변경` | t 는 KST 2026-01-01 00:00 부터의 초. 샘플에서는 t=0 이 2026-11-01 이었으나 끝없는 생성에는 고정 기준점이 필요하다 |
| 부하 변경 | 하루에 한 번, 시드로 정한 시각·방향·길이(4~10시간)로 `±0.018·(tanh 상승 − tanh 하강)/2` | 샘플의 일회성 tanh 구간(52000~81000초)을 "하루 한 번 완만한 정상 부하 변경"으로 일반화. **내 판단** |
| 외기 `A(t)` | `2.2·sin(2π(s−22000)/86400) + OU(sd .08, τ 900 s)` | s 는 KST 하루 중 초. 정오 무렵 최고 |
| 설비 부하 `local_e(t)` | `L(t) + OU(sd .002, τ 500 s)` | 시드는 설비의 `seed_key`. 전역 `L`, `A` 는 상수 키 |
| 축 속도 | 구동기 설비의 `speed` 태그 식을 피구동 설비가 그대로 쓴다 | `driven_by` 관계로 찾는다. B-1201←E-1202, C-1401←T-1401 |

**태그 종류별 식** (`b` 기준값, `s` 잡음, `z = AR(s, τ 6 s) + N(0, 0.12·s)`)

| signal | 식 | 정지 상태 값 |
| --- | --- | --- |
| `vibration` (케이싱 진동) | `b·(1 + 0.7·local) + z + OU(0.6·s, τ 700 s)` | 0 |
| `shaft_vibration` (축 X/Y 상대 진동) | 위와 같음 | 0 |
| `bearing_temp`, `suction_temp`, `oil_temp` | `b + LP₂₄₀(3·local + 0.45·A) + AR(s, τ 80 s)` | 외기 25 °C |
| `discharge_temp` | `b + LP₂₄₀(14·local + 0.6·A) + AR(s, τ 80 s)` | 외기 25 °C |
| `flow` | `b·(1 + local) + z` | 0 |
| `suction_pressure` | `b − 0.12·local + z` | 0 |
| `discharge_pressure` | 펌프: `b − (b − b_suction)·0.4·local + z` (같은 설비의 `suction_pressure` 기준값. 없으면 아래 식) / 그 외: `b·(1 + 0.2·local) + z` | 0 |
| `current` | `b·(1 + 0.75·local) + z` | 0 |
| `speed` | 공기축(blower/expander): `5200·(1 + 0.12·L) + AR(1.2, τ 5 s)`, 가스축: `8200·(1 + 0.25·L) + AR(1.5, τ 5 s)`. 일반화: `b·(1 + k·L) + AR(s, τ 5 s)`, k 는 설비 유형이 blower/expander 면 0.12, 아니면 0.25 | 0 |
| `axial_position` | `b + 0.005·local + AR(s, τ 12 s)` | b |
| `oil_pressure` | `b − 0.008·A + z` | 0 |
| `steam_pressure` | `b·(1 + 0.07·local) + z` | 0 |

`LP₂₄₀` 는 시정수 240초 1차 지연. 설비 유형(펌프/공기축)은 `equipment_types` 의 `type` 이 아니라 설비 유형 항목에 새로 두는 `kind`(pump / blower / expander / compressor / turbine)로 본다. `kind` 는 `EquipmentType` 의 선택 필드다.

**순수 함수로 만드는 방법** (샘플 생성기는 처음부터 끝까지 순차 필터라 그대로 쓸 수 없다)

- 느린 성분(τ ≥ 500 s: `L` 의 OU, `A` 의 OU, `local`, 진동의 τ 700 OU): 고정 기준 시각(KST 2026-01-01)부터 **600초 격자**의 정상 AR(1) 매듭으로 만들고 사이는 선형 보간한다. 기존 `_drift` 와 같은 방식이다. 시드는 `(seed, 키, 성분 번호)`. 매듭은 필요한 만큼 늘려 캐시한다. 기준 시각 이전 시각을 요청하면 `ConfigError` 로 멈춘다(백필 기간이 2026-01-01 을 넘지 않는다).
- 빠른 성분(τ ≤ 80 s): 기존 `_noise` 처럼 **하루 블록**을 `(seed, seed_key, 성분 번호, 날짜)` 로 시드해 만든다. AR(1) 은 길이 5τ 로 자른 지수 커널과의 `np.convolve` 로 계산한다(블록 앞에 커널 길이만큼 여분 잡음을 둬 블록 시작에서도 정상 상태). 날짜 경계에서 독립 표본이 되지만 진폭이 `s` 수준이라 1초 변동에 묻힌다.
- 온도의 `LP₂₄₀`: 강제항(`local`, `A`)이 보간된 느린 성분이므로 10초 격자로 평가하고 길이 1200초 지수 커널과 합성곱한다. 블록 앞 1200초를 여분으로 평가해 경계에서 끊기지 않는다.
- 태그별 블록은 그 태그의 `interval_sec` 격자로만 만든다(온도는 하루 8640개). 캐시는 태그당 최근 2일.

메모리: 65개 1초 태그 × 86400 × 8 B ≈ 45 MB/일. 2일 캐시 ≈ 90 MB. 허용.

### 4.3 시나리오 확장

| 항목 | 변경 |
| --- | --- |
| 패턴 `swell` (`simulator/patterns.py`) | 샘플의 `envelope()`: 진행률 z<0.25 는 `sin²(πz/0.5)` 로 올라가고, 0.25~0.7 은 1, 0.7~1 은 `cos²(π(z−0.7)/0.6)` 로 내려온다. `magnitude × envelope` |
| `ScenarioEvent.repeat_every` (`common/scenario_models.py`) | 선택. `7d` 같은 기간. `start + k·repeat_every` (k ≥ 0) 마다 같은 이벤트가 다시 일어난다. `recovered_at` 과 함께 쓸 수 없다(검증). `duration ≤ repeat_every` |
| `seed_history` | 반복 이벤트는 첫 회차만 이력에 넣는다. RFCC 이벤트는 "확정 고장"이 아니므로 `failure_id` 를 주지 않아 이력에 들어가지 않는다 |

`config/scenario_rfcc.yaml` (샘플의 `development_events.json` 과 `generate.py` 의 주입 식을 옮긴다. 시작 시각은 샘플과 같은 KST 시각, 매주 반복)

```yaml
events:
  # DEV-RFCC-001 흡입 조건 악화 의심 (P-1101A) 40분
  - { event_id: EV-RFCC-001, tag_id: VI-P-1101A, pattern: swell, start: 2026-11-02T05:10:00+09:00, duration: 40m,
      repeat_every: 7d, magnitude: 1.85,
      related: [ { tag_id: PS-P-1101A, ratio: -0.4595 },     # −0.85 / 1.85
                 { tag_id: PD-P-1101A, ratio: -0.4865 },     # −0.9 / 1.85
                 { tag_id: FI-P-1101A, ratio: -13.62 },      # −210·0.12 / 1.85
                 { tag_id: II-P-1101A, ratio: -5.486 },      # −145·0.07 / 1.85
                 { tag_id: TI-P-1101A, ratio: 0.6486 } ],    # 1.2 / 1.85
      description: 흡입 압력 감소·유량 감소·Overall 진동 상승 후 완만한 회복 }
  # DEV-RFCC-002 베어링·윤활 상태 점검 필요 (P-1301A) 90분
  - { event_id: EV-RFCC-002, tag_id: VI-P-1301A, pattern: swell, start: 2026-11-03T16:26:40+09:00, duration: 90m,
      repeat_every: 7d, magnitude: 2.1,
      related: [ { tag_id: TI-P-1301A, ratio: 6.19 },        # 13 / 2.1
                 { tag_id: II-P-1301A, ratio: 1.357 } ],     # 190·0.015 / 2.1
      description: 일정 부하에서 진동과 베어링 온도가 완만하게 상승하고 후반 회복 }
  # 센서 품질 Bad 60초 (P-1302A)
  - { event_id: EV-RFCC-003, tag_id: VI-P-1302A, pattern: quality, quality: Bad,
      start: 2026-11-02T23:13:20+09:00, duration: 60s, repeat_every: 7d }
random_anomalies:
  rate_per_tag_day: 0
```

샘플 생성기의 곱셈 잡음(`jitter`)과 온도 쪽 `LP₂₀₀` 는 옮기지 않는다. 포락선 자체가 20분 이상에 걸쳐 완만해 차이가 작다. **내 판단.**
`random_anomalies` 는 한계값(`warn_hi − base`)으로 크기를 정하므로 한계값 없는 태그는 건너뛴다. RFCC 기본은 0.

### 4.4 생성·전송 (`simulator/`)

- `SampleGenerator`: 태그마다 `interval_sec` 격자에 맞는 epoch 만 고른다(`epoch % interval_sec == 0`; KST 는 정시 오프셋이라 UTC 격자와 같다). 값 반올림은 태그의 `decimals`.
- `runtime_rfcc.yaml`: `sample_interval_sec: 1`, `send_interval_sec: 1`, `backfill_days: 1`, `seed: 61109`, `raw_retention_days: 1`, `source_id: SIM-RFCC`.
- 1초 묶음: 매초 약 67개 샘플을 POST. 백필은 지금처럼 1시간 묶음(약 24만 샘플, JSON 약 30 MB). 하루 백필은 24회 요청이다.
- 기존 `plant_hds` + `runtime.yaml` 조합은 그대로 돈다.

### 4.5 수집 서버 (`collector/`)

| 항목 | 변경 |
| --- | --- |
| 보관 삭제 | 매일 배치 → **10분 배치**에서 `ts_raw` 의 `ts < 구간 끝 − raw_retention_days` 행 삭제. `job_state` 의 `daily` 항목은 쓰지 않는다. 하루 보관이면 DB 는 약 500 MB 에서 머문다. SQLite 파일은 줄지 않고 최고점에서 고정된다 |
| 알람 | `limits` 가 없는 태그는 알람 판정에서 제외 |
| NoData 판정 | `3 × max(send_interval_sec, 태그 interval_sec)` 를 넘으면 NoData. 10초 태그가 1초 전송에서 늘 NoData 로 보이는 것을 막는다. plant_hds(전송 60초 > 태그 10초)는 결과가 그대로다 |
| `ingest_batch` 정리 | 10분 배치에서 `received_at` 이 보관 기간을 넘은 행 삭제. 1초 묶음이면 하루 86,400행이라 지우지 않으면 DB 가 고정되지 않는다. 그보다 오래된 묶음은 어차피 측정 시각이 보관 기간 밖이라 거절되므로 `batch_id` 중복 확인 창이 줄어도 결과는 같다 |
| `GET /equipment` | 새 조회. 계층 순서대로 설비 목록과 각 설비의 태그(`tag_id, signal, unit, description, interval_sec`). 화면이 표를 그릴 때 쓴다 |
| `GET /current` | 새 조회. `tag_current` 전체 행 (+ `status`, `/tags/{id}/current` 와 같은 규칙) |
| `GET /` | `collector/static/index.html` 을 돌려준다 |
| `/health` | 변경 없음 |

### 4.6 디버깅 화면 (`collector/static/index.html`, 파일 하나)

- 의존성 없는 HTML + JS. 빌드 없음.
- 열리면 `/equipment`, `/current` 로 표를 채우고 `/stream` SSE 를 구독한다. 샘플이 오면 해당 행의 값·품질·측정 시각을 바꾸고 0.5초 동안 배경을 바꿔 갱신을 표시한다.
- 상단: 수집 서버 `sim_now`(가장 최근 측정 시각, 1초마다 `/health` 조회가 아니라 SSE 샘플의 최대 시각으로), SSE 연결 상태.
- 본문: 설비마다 표 하나. 제목 `설비 ID · 이름 · Unit`. 열: 태그 ID, 설명, 값, 단위, 품질, 측정 시각(KST), 주기.
- 품질 Bad 는 값 칸을 비우고 행을 흐리게. 끊기면 SSE 가 자동 재연결한다(EventSource 기본 동작).

### 4.7 실행·문서

- `docker-compose.yml`: `PLANT_CONFIG`, `RUNTIME_CONFIG`, `SCENARIO_CONFIG`, `DB_PATH` 환경 변수로 파일을 고르고 기본은 RFCC(`plant_rfcc.yaml`, `runtime_rfcc.yaml`, `scenario_rfcc.yaml`, `data/rfcc.db`). `SIM_TIME_MODE` 기본은 `backfill`(하루를 채우고 실시간). 두 서비스에 같은 변수.
- `.gitignore` 에 `rfcc-20261109/` 추가.
- README: RFCC 구성안 절, 새 API 둘, `/` 화면, 보관 규칙 변경, 설정 필드 추가분.

## 5. 검증 (테스트로 확인할 것)

1. `plant_rfcc.yaml` 이 읽히고 설비 12·태그 81, 온도 태그 16개가 `interval_sec: 10`, 나머지 65개가 1.
2. rfcc 모델: 같은 epoch 를 다른 구간으로 잘라 요청해도 값이 같다(순수 함수). 자정 전후 1초 차이가 잡음 범위(진동 10·s 이내). 피구동 설비의 속도가 구동기와 같다. 정지 상태 값.
3. 태그별 주기: 1초 구간을 만들면 10초 태그는 `epoch % 10 == 0` 인 초에만 샘플이 있다. 1시간 백필 구간의 샘플 수 = 65·3600 + 16·360.
4. `swell` 포락선: z=0 → 0, z=0.25 → 1, z=0.5 → 1, z=1 → 0.
5. `repeat_every`: 7일 뒤 같은 시각에 같은 편차. `recovered_at` 과 함께 쓰면 오류.
6. 보관: 10분 배치가 돌 때 하루 넘은 원시 행이 지워지고 집계는 남는다.
7. 한계값 없는 태그는 알람이 생기지 않고 `_evaluate_alarms` 가 예외 없이 돈다.
7a. 10초 태그는 `sim_now` 와 9초 차이에서도 `status` 가 `Good` 이고, 31초 차이면 `NoData`. 보관 기간을 넘은 `ingest_batch` 행이 지워진다.
8. `/equipment`, `/current`, `/` 응답. `/` 는 HTML.
9. 기존 80개 테스트 통과.
10. 실제 실행: RFCC 로 compose 를 띄워 `/` 에서 81행이 1초마다 바뀌는 것을 눈으로 확인하고, 백필 하루가 끝난 뒤 DB 크기를 기록한다.

## 6. 작업자에게 맡기는 것

- 느린 성분 매듭 캐시와 빠른 성분 블록 캐시의 자료구조. 위 수치(격자 600초, 커널 5τ, 캐시 2일)는 지켜야 한다.
- `rfcc_model.py` 안의 함수 분할. 공통 성분(`load`, `ambient`, `speed`)은 설비 간에 공유해야 하므로 모델 객체 하나가 들고 있어야 한다.
- 화면의 표 스타일. 값만 보이면 된다.
- `plant_rfcc.yaml` 의 태그 `seed_key` 는 `r001` ~ `r081`, 설비 `seed_key` 는 `e01` ~ `e12` 처럼 고유하면 된다. Unit 은 샘플 `config.json` 의 ID 8개(`UNIT-RFCC-01`~`08`)를 쓴다(매뉴얼이 이 ID 를 인용한다). `config.json` 의 Unit 이름 칸은 생성 스크립트 오류로 단위 문자열이 들어가 있어 쓰지 않고, 샘플 README 표의 Unit 이름을 쓰되 한 ID 에 여러 설비가 묶인 곳은 이름을 합친다: `UNIT-RFCC-04` = 슬러리·LCO 순환·탑정 처리(P-1301A/1302A/1303A), `UNIT-RFCC-07` = 흡수·분별·안정화(P-1402A/1403A). **내 판단.** `equipment_types` 는 pump(PM) / blower(BL) / expander(EX) / compressor(CC) / turbine(ST) 다섯 개. `aliases` 는 비워 둔다.

## 7. 샘플 자료 ↔ 구성안 대응

| 샘플 `config.json` | `plant_rfcc.yaml` |
| --- | --- |
| `equips[].id / name / group_id / unit_id` | `equip_id / name / unit_id` (unit 은 `unit_groups[].units[]` 에 등록) |
| `equips[].kind` | `equipment_types[].kind` 를 통해 |
| `tags[].name` | `tag_id` |
| `tags[].code` | `signal` (VI→vibration, VX/VY→shaft_vibration, TI→bearing_temp, TS→suction_temp, TD→discharge_temp, LOT→oil_temp, PS→suction_pressure, PD→discharge_pressure, LOP→oil_pressure, SP→steam_pressure, FI→flow, II→current, SI→speed, ZI→axial_position) |
| `tags[].unit / description` | `unit / description` |
| `tags[].baseline / noise` | `normal.base / normal.noise_sd` |
| `tags[].record_seconds` | `interval_sec` |
| B-1201/E-1202, C-1401/T-1401 같은 축 | `driven_by: E-1202`, `driven_by: T-1401` |
