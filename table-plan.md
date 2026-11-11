# 설비 DB 테이블 정리

설비 데이터로 DB 테이블을 구성하기 전에, 지금 어떤 공장·설비·태그가 있고 그 값이 어느 테이블에 들어가는지 정리한다.

- **기준**: 2026-11-11, `main` 기준. 배포 목록 `config/plants.yaml` 의 다섯 공장(RFCC·CDU·HOU·RHDS·VRDS). 수첨탈황(HDS)은 배포에서 빠져 있어 제외했다.
- **출처**: 구성안 `config/plant_<key>.yaml` (각 샘플 Excel `설비 태그` 시트에서 옮긴 마스터), 스키마 `collector/schema.sql` (v2), 적재 코드 `collector/seed_master.py`.
- **현재 구조**: 공장마다 SQLite 파일을 따로 쓴다 (`data/<key>.db`). 테이블 구성은 다섯 파일 모두 같다.
- **결정 (2026-11-11)**: PostgreSQL 한 DB 에 **공장별 스키마**로, **이상이 탐지된 것만 지우지 않고** 담는다. 판정은 한계값 초과 + 품질 이상(Bad/ScanOff), 저장은 이상 행 + 앞뒤 원시 값. 6절 참고.

## 1. 공장 요약

| key | plant_id | 공장 | 설비 | 태그 (1초 / 10초) | 한계값 있는 태그 | 원시 행/일 |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| `rfcc` | `PL-RFCC` | 가상 정유단지 RFCC | 12 | 81 (65 / 16) | 81 (통계) | 약 575만 |
| `cdu` | `PL-CDU` | CDU (상압증류) | 7 | 46 (37 / 9) | 46 | 약 327만 |
| `hou` | `PL-HOU` | HOU (수첨분해) | 7 | 61 (43 / 18) | 61 | 약 387만 |
| `rhds` | `PL-RHDS` | RHDS (상압 잔사유 수첨탈황) | 6 | 43 (31 / 12) | 43 | 약 278만 |
| `vrds` | `PL-VRDS` | VRDS (감압 잔사유 수첨탈황) | 6 | 43 (31 / 12) | 43 | 약 278만 |
| **합계** | | | **38** | **274** | **274** | **약 1,846만** |

- 온도 계열 태그는 10초, 나머지는 1초 주기다. 원시 행/일은 `Σ 86400 / interval_sec` 이다.
- 10분 집계는 태그당 하루 144행, 1시간 집계는 24행이다.

## 2. 테이블 목록

`collector/schema.sql` 의 테이블 전부다. 태그는 **(equip_id, tag_id) 복합키**로 식별한다 (태그명은 공장 안에서 고유하다. RHDS·VRDS 는 자료에서 겹치던 이름에 설비 종류 문자를 넣어 바꿨다: `TI-B-3101` → `TI-B-P3101`·`TI-B-C3101`·`TI-B-F3101`).

### 2.1 마스터 — 구성안에서 서버 시작 때 적재 (`INSERT OR REPLACE`)

| 테이블 | 키 | 주요 컬럼 | 출처 (구성안) |
| --- | --- | --- | --- |
| `schema_meta` | — | `version` (=2) | 스키마 버전 |
| `plant` | `plant_id` | `name`, `config_version` | `plant` |
| `unit_group` | `unit_group_id` | `plant_id`, `name` | `plant.unit_groups` |
| `unit` | `unit_id` | `unit_group_id`, `name` | `plant.unit_groups[].units` |
| `equipment_type` | `type` | `name`, `prefix` | `equipment_types` |
| `equipment` | `equip_id` | `name`, `type`, `grade`, `unit_id`, `driven_by` | `equipment` |
| `equipment_alias` | `alias` | `equip_id` | `equipment[].aliases` |
| `tag_std` | `signal` | `prefix`, `unit`, `name` | `signals` |
| `equipment_tag` | `(equip_id, tag_id)` | `signal`, `unit`, `location`, `description`, `normal_ref`, `limit_basis`, `warn/alarm/trip_lo`, `warn/alarm/trip_hi`, `delay_samples`, `interval_sec`, `decimals` | `equipment[].tags` |

공장별 마스터 행 수:

| key | plant | unit_group | unit | equipment_type | tag_std | equipment | equipment_alias | equipment_tag |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `rfcc` | 1 | 5 | 8 | 5 | 14 | 12 | 0 | 81 |
| `cdu` | 1 | 4 | 7 | 2 | 9 | 7 | 0 | 46 |
| `hou` | 1 | 5 | 7 | 3 | 15 | 7 | 0 | 61 |
| `rhds` | 1 | 6 | 6 | 3 | 15 | 6 | 0 | 43 |
| `vrds` | 1 | 6 | 6 | 3 | 15 | 6 | 0 | 43 |

### 2.2 이력 — 시나리오 파일에서 백필 때 적재 (개발용)

| 테이블 | 키 | 주요 컬럼 | 비고 |
| --- | --- | --- | --- |
| `maintenance_history` | `work_order_id` | `equip_id`, `start_ts`, `end_ts`, `description` | 다섯 공장 시나리오 모두 `maintenance` 없음 → 0행 |
| `failure_history` | `failure_id` | `equip_id`, `tag_id`, `start_ts`, `end_ts`, `work_order_id`, `doc_id`, `description` | 이벤트에 `failure_id` 가 없음 (확정 고장 아님) → 0행 |

### 2.3 시계열 — 수집 서버가 수신·배치로 채움

| 테이블 | 키 | 주요 컬럼 | 채우는 시점 | 보관 |
| --- | --- | --- | --- | --- |
| `ts_raw` | `(equip_id, tag_id, ts)` | `value`, `quality`, `batch_id`, `received_at` | 수신 즉시 | `raw_retention_days` (현재 1일) |
| `tag_current` | `(equip_id, tag_id)` | `ts`, `value`, `quality`, `received_at` | 수신 즉시 | 태그당 1행 |
| `ts_10m` | `(equip_id, tag_id, bucket_start)` | `avg`, `min`, `max`, `sd`, `count`, `status` | 10분 배치 | 계속 |
| `ts_1h` | `(equip_id, tag_id, bucket_start)` | `avg`, `min`, `max`, `sd`, `count`, `status` | 매시 배치 | 계속 |
| `alarm_detail` | `alarm_id` (자동 증가) | `occurred_at`, `cleared_at`, `equip_id`, `tag_id`, `limit_type`, `direction`, `limit_value`, `actual_value`, `exceed_pct`, `severity` | 10분 배치 | 계속 |

- `quality`: Good / Uncertain / Bad / ScanOff. `limit_type`: Soft / Alarm / Trip. `direction`: hi / lo. `severity`: Trip→High, Alarm→Medium, Soft→Low.
- RFCC 는 자료에 한계값이 없어 통계 경고 한계를 쓴다: 정상 구간 10분 최대·최소의 평균 ± 3σ, `delay_samples` 2. 시뮬레이터 정상 7일에서 잡음만으로 연속 2구간을 넘은 건 6건.

### 2.4 운영 — 수집 서버 내부

| 테이블 | 키 | 주요 컬럼 | 용도 |
| --- | --- | --- | --- |
| `ingest_batch` | `batch_id` | `source_id`, `received_at`, `stored` | 수신 묶음 중복 확인 |
| `job_state` | `name` | `last_bucket_end` | 배치 진행 위치 |

### 2.5 이 DB 에 없는 테이블

기획서 7.1 의 Skadi 쪽 테이블(지식 문서·분할 본문, 대화 세션, 보고서, 평가 결과)과 정답 세트 `golden_case` 는 앞단 범위 밖이라 만들지 않았다.

## 3. 공장별 설비·태그

- 유형: PM 펌프, FN 팬, CC 압축기, BL 송풍기, EX 팽창 터빈, ST 증기 터빈.
- 한계값 칸은 `경고 / 알람 / 정지` 순서이고, `—` 는 자료에 값이 없는 칸이다 (DB 에는 NULL).

### 3.1 가상 정유단지 RFCC — `rfcc` (`PL-RFCC`)

설비 12대 · 태그 81개

| 설비 | 이름 | 유형 | 공정 그룹 / 단위 | 태그 수 |
| --- | --- | --- | --- | ---: |
| `P-1101A` | Feed Charge Pump | PM 펌프 | 원료 공급 / 원료 공급 | 6 |
| `B-1201` | Main Air Blower (구동: E-1202) | BL 송풍기 | 반응·재생 / 촉매 재생 | 8 |
| `E-1202` | Power Recovery Expander | EX 팽창 터빈 | 반응·재생 / 동력 회수 | 8 |
| `P-1301A` | Slurry Pumparound Pump | PM 펌프 | 주분별 / 슬러리·LCO 순환·탑정 처리 | 6 |
| `P-1302A` | LCO Pumparound Pump | PM 펌프 | 주분별 / 슬러리·LCO 순환·탑정 처리 | 6 |
| `P-1303A` | Overhead Reflux Pump | PM 펌프 | 주분별 / 슬러리·LCO 순환·탑정 처리 | 6 |
| `C-1401` | Wet Gas Compressor (구동: T-1401) | CC 원심 압축기 | 가스 압축·회수 / 습가스 압축 | 10 |
| `T-1401` | Wet Gas Compressor Driver Steam Turbine | ST 증기 터빈 | 가스 압축·회수 / 습가스 압축 구동 | 7 |
| `P-1402A` | Absorber Oil Pump | PM 펌프 | 가스 압축·회수 / 흡수·분별·안정화 | 6 |
| `P-1403A` | Gas Recovery Reflux Pump | PM 펌프 | 가스 압축·회수 / 흡수·분별·안정화 | 6 |
| `P-1501A` | LPG Transfer Pump | PM 펌프 | 제품 처리 / LPG·가솔린 처리 | 6 |
| `P-1502A` | Gasoline Transfer Pump | PM 펌프 | 제품 처리 / LPG·가솔린 처리 | 6 |

<details><summary>태그 목록 (한계값: 경고 / 알람 / 정지)</summary>

| 설비 | 태그 | 신호 | 단위 | 설명 | 주기(s) | 하한 | 상한 |
| --- | --- | --- | --- | --- | ---: | --- | --- |
| `P-1101A` | `VI-P-1101A` | vibration | mm/s RMS | 펌프 구동측 케이싱 진동 속도 Overall RMS | 1 | 1.2567 / — / — | 1.6451 / — / — |
| `P-1101A` | `TI-P-1101A` | bearing_temp | degC | 펌프 구동측 베어링 온도 | 10 | 63.7601 / — / — | 68.2231 / — / — |
| `P-1101A` | `PS-P-1101A` | suction_pressure | bar(g) | 흡입 압력 | 1 | 3.1645 / — / — | 3.2353 / — / — |
| `P-1101A` | `PD-P-1101A` | discharge_pressure | bar(g) | 토출 압력 | 1 | 18.5441 / — / — | 19.4206 / — / — |
| `P-1101A` | `FI-P-1101A` | flow | m3/h | 액체 체적 유량 | 1 | 197.3323 / — / — | 223.8069 / — / — |
| `P-1101A` | `II-P-1101A` | current | A | 구동 모터 전류 | 1 | 138.4174 / — / — | 152.1602 / — / — |
| `B-1201` | `VX-B-1201` | shaft_vibration | um p-p | 구동측 X축 상대 진동 | 1 | 28.2733 / — / — | 35.8724 / — / — |
| `B-1201` | `VY-B-1201` | shaft_vibration | um p-p | 구동측 Y축 상대 진동 | 1 | 25.3324 / — / — | 32.7922 / — / — |
| `B-1201` | `TI-B-1201` | bearing_temp | degC | 구동측 저널 베어링 온도 | 10 | 75.7936 / — / — | 80.2017 / — / — |
| `B-1201` | `PS-B-1201` | suction_pressure | bar(g) | 입구 공기 압력 | 1 | -0.0248 / — / — | -0.0059 / — / — |
| `B-1201` | `PD-B-1201` | discharge_pressure | bar(g) | 토출 공기 압력 | 1 | 2.7332 / — / — | 2.8715 / — / — |
| `B-1201` | `FI-B-1201` | flow | Nm3/h | 표준 상태 공기 체적 유량 | 1 | 170207.0328 / — / — | 190933.9342 / — / — |
| `B-1201` | `SI-B-1201` | speed | rpm | 축 회전속도 | 1 | 5164.3385 / — / — | 5239.1692 / — / — |
| `B-1201` | `LOP-B-1201` | oil_pressure | bar(g) | 윤활유 공급 압력 | 1 | 2.151 / — / — | 2.2495 / — / — |
| `E-1202` | `VX-E-1202` | shaft_vibration | um p-p | 구동측 X축 상대 진동 | 1 | 30.9272 / — / — | 39.1313 / — / — |
| `E-1202` | `VY-E-1202` | shaft_vibration | um p-p | 구동측 Y축 상대 진동 | 1 | 27.9967 / — / — | 36.1586 / — / — |
| `E-1202` | `TI-E-1202` | bearing_temp | degC | 저널 베어링 온도 | 10 | 79.796 / — / — | 84.1938 / — / — |
| `E-1202` | `ZI-E-1202` | axial_position | mm | 축방향 상대 위치 | 1 | 0.03664 / — / — | 0.04331 / — / — |
| `E-1202` | `SI-E-1202` | speed | rpm | 축 회전속도 | 1 | 5164.3385 / — / — | 5239.1692 / — / — |
| `E-1202` | `PS-E-1202` | suction_pressure | bar(g) | 배기가스 입구 압력 | 1 | 2.5482 / — / — | 2.6518 / — / — |
| `E-1202` | `PD-E-1202` | discharge_pressure | bar(g) | 배기가스 출구 압력 | 1 | 0.1172 / — / — | 0.1426 / — / — |
| `E-1202` | `LOT-E-1202` | oil_temp | degC | 윤활유 공급 온도 | 10 | 42.8124 / — / — | 47.1791 / — / — |
| `P-1301A` | `VI-P-1301A` | vibration | mm/s RMS | 펌프 구동측 케이싱 진동 속도 Overall RMS | 1 | 1.629 / — / — | 2.0749 / — / — |
| `P-1301A` | `TI-P-1301A` | bearing_temp | degC | 펌프 구동측 베어링 온도 | 10 | 71.7556 / — / — | 76.2255 / — / — |
| `P-1301A` | `PS-P-1301A` | suction_pressure | bar(g) | 흡입 압력 | 1 | 4.0632 / — / — | 4.1345 / — / — |
| `P-1301A` | `PD-P-1301A` | discharge_pressure | bar(g) | 토출 압력 | 1 | 10.2804 / — / — | 10.709 / — / — |
| `P-1301A` | `FI-P-1301A` | flow | m3/h | 액체 체적 유량 | 1 | 357.8561 / — / — | 403.4699 / — / — |
| `P-1301A` | `II-P-1301A` | current | A | 구동 모터 전류 | 1 | 181.6953 / — / — | 198.8389 / — / — |
| `P-1302A` | `VI-P-1302A` | vibration | mm/s RMS | 펌프 구동측 케이싱 진동 속도 Overall RMS | 1 | 0.9997 / — / — | 1.4417 / — / — |
| `P-1302A` | `TI-P-1302A` | bearing_temp | degC | 펌프 구동측 베어링 온도 | 10 | 65.7997 / — / — | 70.1955 / — / — |
| `P-1302A` | `PS-P-1302A` | suction_pressure | bar(g) | 흡입 압력 | 1 | 2.9642 / — / — | 3.0349 / — / — |
| `P-1302A` | `PD-P-1302A` | discharge_pressure | bar(g) | 토출 압력 | 1 | 9.2769 / — / — | 9.7067 / — / — |
| `P-1302A` | `FI-P-1302A` | flow | m3/h | 액체 체적 유량 | 1 | 226.4188 / — / — | 254.8523 / — / — |
| `P-1302A` | `II-P-1302A` | current | A | 구동 모터 전류 | 1 | 93.8206 / — / — | 102.5589 / — / — |
| `P-1303A` | `VI-P-1303A` | vibration | mm/s RMS | 펌프 구동측 케이싱 진동 속도 Overall RMS | 1 | 0.8278 / — / — | 1.2724 / — / — |
| `P-1303A` | `TI-P-1303A` | bearing_temp | degC | 펌프 구동측 베어링 온도 | 10 | 55.7951 / — / — | 60.196 / — / — |
| `P-1303A` | `PS-P-1303A` | suction_pressure | bar(g) | 흡입 압력 | 1 | 2.4643 / — / — | 2.5352 / — / — |
| `P-1303A` | `PD-P-1303A` | discharge_pressure | bar(g) | 토출 압력 | 1 | 7.9899 / — / — | 8.3939 / — / — |
| `P-1303A` | `FI-P-1303A` | flow | m3/h | 액체 체적 유량 | 1 | 127.205 / — / — | 143.5388 / — / — |
| `P-1303A` | `II-P-1303A` | current | A | 구동 모터 전류 | 1 | 62.1688 / — / — | 68.0888 / — / — |
| `C-1401` | `VX-C-1401` | shaft_vibration | um p-p | 구동측 X축 상대 진동 | 1 | 26.4709 / — / — | 33.7498 / — / — |
| `C-1401` | `VY-C-1401` | shaft_vibration | um p-p | 구동측 Y축 상대 진동 | 1 | 23.366 / — / — | 30.7156 / — / — |
| `C-1401` | `TI-C-1401` | bearing_temp | degC | 구동측 저널 베어링 온도 | 10 | 74.7974 / — / — | 79.1987 / — / — |
| `C-1401` | `ZI-C-1401` | axial_position | mm | 축방향 상대 위치 | 1 | 0.02665 / — / — | 0.03338 / — / — |
| `C-1401` | `PS-C-1401` | suction_pressure | bar(g) | 1단 흡입 압력 | 1 | 1.1719 / — / — | 1.2274 / — / — |
| `C-1401` | `PD-C-1401` | discharge_pressure | bar(g) | 최종 토출 압력 | 1 | 12.2843 / — / — | 12.7248 / — / — |
| `C-1401` | `TS-C-1401` | suction_temp | degC | 1단 흡입 온도 | 10 | 39.784 / — / — | 44.1997 / — / — |
| `C-1401` | `TD-C-1401` | discharge_temp | degC | 최종 토출 온도 | 10 | 114.8455 / — / — | 121.1934 / — / — |
| `C-1401` | `FI-C-1401` | flow | Nm3/h | 흡입 가스 표준 상태 체적 유량 | 1 | 39654.5459 / — / — | 44615.3311 / — / — |
| `C-1401` | `SI-C-1401` | speed | rpm | 축 회전속도 | 1 | 8088.6267 / — / — | 8322.8262 / — / — |
| `T-1401` | `VX-T-1401` | shaft_vibration | um p-p | 구동측 X축 상대 진동 | 1 | 24.6318 / — / — | 31.5256 / — / — |
| `T-1401` | `VY-T-1401` | shaft_vibration | um p-p | 구동측 Y축 상대 진동 | 1 | 21.7343 / — / — | 28.4011 / — / — |
| `T-1401` | `TI-T-1401` | bearing_temp | degC | 구동측 저널 베어링 온도 | 10 | 77.802 / — / — | 82.1965 / — / — |
| `T-1401` | `SI-T-1401` | speed | rpm | 축 회전속도 | 1 | 8088.6267 / — / — | 8322.8262 / — / — |
| `T-1401` | `LOP-T-1401` | oil_pressure | bar(g) | 윤활유 공급 압력 | 1 | 2.0511 / — / — | 2.1495 / — / — |
| `T-1401` | `LOT-T-1401` | oil_temp | degC | 윤활유 공급 온도 | 10 | 42.8162 / — / — | 47.1753 / — / — |
| `T-1401` | `SP-T-1401` | steam_pressure | bar(g) | 입구 증기 압력 | 1 | 39.7312 / — / — | 40.2899 / — / — |
| `P-1402A` | `VI-P-1402A` | vibration | mm/s RMS | 펌프 구동측 케이싱 진동 속도 Overall RMS | 1 | 0.8792 / — / — | 1.3674 / — / — |
| `P-1402A` | `TI-P-1402A` | bearing_temp | degC | 펌프 구동측 베어링 온도 | 10 | 60.8061 / — / — | 65.1909 / — / — |
| `P-1402A` | `PS-P-1402A` | suction_pressure | bar(g) | 흡입 압력 | 1 | 7.1636 / — / — | 7.2351 / — / — |
| `P-1402A` | `PD-P-1402A` | discharge_pressure | bar(g) | 토출 압력 | 1 | 16.701 / — / — | 17.2778 / — / — |
| `P-1402A` | `FI-P-1402A` | flow | m3/h | 액체 체적 유량 | 1 | 89.5748 / — / — | 100.9547 / — / — |
| `P-1402A` | `II-P-1402A` | current | A | 구동 모터 전류 | 1 | 49.7696 / — / — | 54.4512 / — / — |
| `P-1403A` | `VI-P-1403A` | vibration | mm/s RMS | 펌프 구동측 케이싱 진동 속도 Overall RMS | 1 | 0.7278 / — / — | 1.2282 / — / — |
| `P-1403A` | `TI-P-1403A` | bearing_temp | degC | 펌프 구동측 베어링 온도 | 10 | 56.7938 / — / — | 61.1985 / — / — |
| `P-1403A` | `PS-P-1403A` | suction_pressure | bar(g) | 흡입 압력 | 1 | 9.9637 / — / — | 10.0357 / — / — |
| `P-1403A` | `PD-P-1403A` | discharge_pressure | bar(g) | 토출 압력 | 1 | 17.743 / — / — | 18.2412 / — / — |
| `P-1403A` | `FI-P-1403A` | flow | m3/h | 액체 체적 유량 | 1 | 77.3507 / — / — | 87.1257 / — / — |
| `P-1403A` | `II-P-1403A` | current | A | 구동 모터 전류 | 1 | 46.8999 / — / — | 51.3067 / — / — |
| `P-1501A` | `VI-P-1501A` | vibration | mm/s RMS | 펌프 구동측 케이싱 진동 속도 Overall RMS | 1 | 0.826 / — / — | 1.348 / — / — |
| `P-1501A` | `TI-P-1501A` | bearing_temp | degC | 펌프 구동측 베어링 온도 | 10 | 52.7974 / — / — | 57.1983 / — / — |
| `P-1501A` | `PS-P-1501A` | suction_pressure | bar(g) | 흡입 압력 | 1 | 11.9641 / — / — | 12.0355 / — / — |
| `P-1501A` | `PD-P-1501A` | discharge_pressure | bar(g) | 토출 압력 | 1 | 19.7424 / — / — | 20.2412 / — / — |
| `P-1501A` | `FI-P-1501A` | flow | m3/h | 액체 체적 유량 | 1 | 61.3325 / — / — | 69.0746 / — / — |
| `P-1501A` | `II-P-1501A` | current | A | 구동 모터 전류 | 1 | 42.1326 / — / — | 46.0694 / — / — |
| `P-1502A` | `VI-P-1502A` | vibration | mm/s RMS | 펌프 구동측 케이싱 진동 속도 Overall RMS | 1 | 0.9186 / — / — | 1.4577 / — / — |
| `P-1502A` | `TI-P-1502A` | bearing_temp | degC | 펌프 구동측 베어링 온도 | 10 | 54.7974 / — / — | 59.1941 / — / — |
| `P-1502A` | `PS-P-1502A` | suction_pressure | bar(g) | 흡입 압력 | 1 | 2.0639 / — / — | 2.1353 / — / — |
| `P-1502A` | `PD-P-1502A` | discharge_pressure | bar(g) | 토출 압력 | 1 | 8.2766 / — / — | 8.7069 / — / — |
| `P-1502A` | `FI-P-1502A` | flow | m3/h | 액체 체적 유량 | 1 | 113.2339 / — / — | 127.48 / — / — |
| `P-1502A` | `II-P-1502A` | current | A | 구동 모터 전류 | 1 | 57.4575 / — / — | 62.8178 / — / — |

</details>

### 3.2 CDU (상압증류) — `cdu` (`PL-CDU`)

설비 7대 · 태그 46개

| 설비 | 이름 | 유형 | 공정 그룹 / 단위 | 태그 수 |
| --- | --- | --- | --- | ---: |
| `P-8101A` | 원유 공급 펌프 A (Crude Charge Pump A) | PM 펌프 | 원유 전처리 / U-811 | 7 |
| `P-8102A` | 탈염 세정수 펌프 A (Desalter Wash Water Pump A) | PM 펌프 | 원유 전처리 / U-812 | 6 |
| `F-8201` | 가열로 강제 통풍 팬 (Crude Heater FD Fan) | FN 팬 | 원유 가열 / U-821 | 7 |
| `P-8301A` | 중간 순환환류 펌프 A (Middle Pumparound Pump A) | PM 펌프 | 상압증류 / U-831 | 7 |
| `P-8302A` | 탑정 환류 펌프 A (Overhead Reflux Pump A) | PM 펌프 | 상압증류 / U-832 | 6 |
| `P-8303A` | 상압탑 하부 펌프 A (Atmospheric Bottoms Pump A) | PM 펌프 | 상압증류 / U-833 | 7 |
| `P-8401A` | 경유 제품 이송 펌프 A (Diesel Product Transfer Pump A) | PM 펌프 | 제품 회수 / U-841 | 6 |

<details><summary>태그 목록 (한계값: 경고 / 알람 / 정지)</summary>

| 설비 | 태그 | 신호 | 단위 | 설명 | 주기(s) | 하한 | 상한 |
| --- | --- | --- | --- | --- | ---: | --- | --- |
| `P-8101A` | `VI-8101A` | vibration | mm/s RMS | 펌프 구동측 진동 | 1 | — | 3 / 4.5 / 7.1 |
| `P-8101A` | `VI-8101B` | vibration | mm/s RMS | 펌프 반구동측 진동 | 1 | — | 3 / 4.5 / 7.1 |
| `P-8101A` | `TI-8101A` | bearing_temp | DEG C | 펌프 베어링 온도 | 10 | — | 80 / 90 / 100 |
| `P-8101A` | `PI-8101A` | suction_pressure | kg/cm2(g) | 펌프 흡입 압력 | 1 | 2.02 / 1.62 / — | — |
| `P-8101A` | `PI-8101B` | discharge_pressure | kg/cm2(g) | 펌프 토출 압력 | 1 | 20.16 / 16.24 / — | — |
| `P-8101A` | `FI-8101A` | flow | m3/h | 원유 공급 유량 | 1 | 288 / 248 / — | — |
| `P-8101A` | `II-8101A` | current | A | 모터 전류 | 1 | — | 122 / 129 / — |
| `P-8102A` | `VI-8102A` | vibration | mm/s RMS | 펌프 구동측 진동 | 1 | — | 3 / 4.5 / 7.1 |
| `P-8102A` | `VI-8102B` | vibration | mm/s RMS | 펌프 반구동측 진동 | 1 | — | 3 / 4.5 / 7.1 |
| `P-8102A` | `TI-8102A` | bearing_temp | DEG C | 펌프 베어링 온도 | 10 | — | 80 / 90 / 100 |
| `P-8102A` | `PI-8102B` | discharge_pressure | kg/cm2(g) | 펌프 토출 압력 | 1 | 12.6 / 10.15 / — | — |
| `P-8102A` | `FI-8102A` | flow | m3/h | 세정수 유량 | 1 | 29 / 25 / — | — |
| `P-8102A` | `II-8102A` | current | A | 모터 전류 | 1 | — | 174 / 184 / — |
| `F-8201` | `VI-8201A` | vibration | mm/s RMS | 팬 구동측 진동 | 1 | — | 4.5 / 7.1 / 11 |
| `F-8201` | `VI-8201B` | vibration | mm/s RMS | 팬 반구동측 진동 | 1 | — | 4.5 / 7.1 / 11 |
| `F-8201` | `TI-8201A` | bearing_temp | DEG C | 팬 베어링 온도 | 10 | — | 80 / 90 / 100 |
| `F-8201` | `SI-8201A` | speed | rpm | 회전속도 | 1 | — | 1250 / 1300 / — |
| `F-8201` | `II-8201A` | current | A | 모터 전류 | 1 | — | 53 / 57 / — |
| `F-8201` | `FI-8201A` | flow | Nm3/h | 연소 공기 유량 | 1 | 85000 / 75000 / — | — |
| `F-8201` | `PDI-8201A` | differential_pressure | mmH2O | 팬 전후 차압 | 1 | 140 / 120 / — | 240 / — / — |
| `P-8301A` | `VI-8301A` | vibration | mm/s RMS | 펌프 구동측 진동 | 1 | — | 3 / 4.5 / 7.1 |
| `P-8301A` | `VI-8301B` | vibration | mm/s RMS | 펌프 반구동측 진동 | 1 | — | 3 / 4.5 / 7.1 |
| `P-8301A` | `TI-8301A` | bearing_temp | DEG C | 펌프 베어링 온도 | 10 | — | 80 / 90 / 100 |
| `P-8301A` | `FI-8301A` | flow | m3/h | 중간 순환환류 유량 | 1 | 374 / 322 / — | — |
| `P-8301A` | `PI-8301A` | suction_pressure | kg/cm2(g) | 펌프 흡입 압력 | 1 | 1.87 / 1.51 / — | — |
| `P-8301A` | `PI-8301B` | discharge_pressure | kg/cm2(g) | 펌프 토출 압력 | 1 | 6.48 / 5.22 / — | — |
| `P-8301A` | `TI-8301B` | process_temp | DEG C | 인출 유체 온도 | 10 | — | 235 / 245 / — |
| `P-8302A` | `VI-8302A` | vibration | mm/s RMS | 펌프 구동측 진동 | 1 | — | 3 / 4.5 / 7.1 |
| `P-8302A` | `VI-8302B` | vibration | mm/s RMS | 펌프 반구동측 진동 | 1 | — | 3 / 4.5 / 7.1 |
| `P-8302A` | `TI-8302A` | bearing_temp | DEG C | 펌프 베어링 온도 | 10 | — | 80 / 90 / 100 |
| `P-8302A` | `PI-8302A` | suction_pressure | kg/cm2(g) | 펌프 흡입 압력 | 1 | 0.68 / 0.55 / — | — |
| `P-8302A` | `PI-8302B` | discharge_pressure | kg/cm2(g) | 펌프 토출 압력 | 1 | 4.68 / 3.77 / — | — |
| `P-8302A` | `FI-8302A` | flow | m3/h | 탑정 환류 유량 | 1 | 216 / 186 / — | — |
| `P-8303A` | `VI-8303A` | vibration | mm/s RMS | 펌프 구동측 진동 | 1 | — | 3 / 4.5 / 7.1 |
| `P-8303A` | `VI-8303B` | vibration | mm/s RMS | 펌프 반구동측 진동 | 1 | — | 3 / 4.5 / 7.1 |
| `P-8303A` | `TI-8303A` | bearing_temp | DEG C | 펌프 베어링 온도 | 10 | — | 80 / 90 / 100 |
| `P-8303A` | `TI-8303B` | process_temp | DEG C | 흡입 유체 온도 | 10 | — | 360 / 370 / — |
| `P-8303A` | `PI-8303A` | suction_pressure | kg/cm2(g) | 펌프 흡입 압력 | 1 | 1.51 / 1.22 / — | — |
| `P-8303A` | `PI-8303B` | discharge_pressure | kg/cm2(g) | 펌프 토출 압력 | 1 | 10.08 / 8.12 / — | — |
| `P-8303A` | `FI-8303A` | flow | m3/h | 상압 잔사유 유량 | 1 | 259 / 223 / — | — |
| `P-8401A` | `VI-8401A` | vibration | mm/s RMS | 펌프 구동측 진동 | 1 | — | 3 / 4.5 / 7.1 |
| `P-8401A` | `VI-8401B` | vibration | mm/s RMS | 펌프 반구동측 진동 | 1 | — | 3 / 4.5 / 7.1 |
| `P-8401A` | `TI-8401A` | bearing_temp | DEG C | 펌프 베어링 온도 | 10 | — | 80 / 90 / 100 |
| `P-8401A` | `FI-8401A` | flow | m3/h | 경유 제품 이송 유량 | 1 | 151 / 130 / — | — |
| `P-8401A` | `PI-8401B` | discharge_pressure | kg/cm2(g) | 펌프 토출 압력 | 1 | 7.56 / 6.09 / — | — |
| `P-8401A` | `II-8401A` | current | A | 모터 전류 | 1 | — | 46 / 49 / — |

</details>

### 3.3 HOU (수첨분해) — `hou` (`PL-HOU`)

설비 7대 · 태그 61개

| 설비 | 이름 | 유형 | 공정 그룹 / 단위 | 태그 수 |
| --- | --- | --- | --- | ---: |
| `P-2101` | 고압 원료 공급 펌프 (High Pressure Feed Pump) | PM 펌프 | 원료 공급 / 원료 가압·공급 | 8 |
| `C-2201` | 보충 수소 압축기 (Make-up Hydrogen Compressor) | CC 압축기 | 수소 공급 / 보충 수소 압축 | 9 |
| `C-2301` | 수소 순환 압축기 (Recycle Gas Compressor) | CC 압축기 | 수첨반응 / 수소 순환 | 13 |
| `FN-2401` | 반응기 가열로 연소공기·배기 팬 (FD/ID Fan) | FN 팬 | 수첨반응 / 반응기 가열 | 8 |
| `P-2501` | 압축기 윤활유 펌프 (Lube Oil Pump) | PM 펌프 | 기액 분리·순환 / 압축기 지원 | 6 |
| `P-2601` | 분별탑 환류 펌프 (Reflux Pump) | PM 펌프 | 생성물 분별 / 분별탑 환류 | 8 |
| `P-2701` | 미전환유 순환 펌프 (Unconverted Oil Recycle Pump) | PM 펌프 | 생성물 분별 / 미전환유 순환 | 9 |

<details><summary>태그 목록 (한계값: 경고 / 알람 / 정지)</summary>

| 설비 | 태그 | 신호 | 단위 | 설명 | 주기(s) | 하한 | 상한 |
| --- | --- | --- | --- | --- | ---: | --- | --- |
| `P-2101` | `VI-2101A` | vibration | mm/s RMS | 펌프 구동측 진동 | 1 | — | 3.5 / 4.5 / 7.1 |
| `P-2101` | `VI-2101B` | vibration | mm/s RMS | 펌프 반구동측 진동 | 1 | — | 3.5 / 4.5 / 7.1 |
| `P-2101` | `TI-2101A` | bearing_temp | DEG C | 펌프 구동측 베어링 온도 | 10 | — | 80 / 90 / 100 |
| `P-2101` | `TI-2101B` | bearing_temp | DEG C | 펌프 반구동측 베어링 온도 | 10 | — | 80 / 90 / 100 |
| `P-2101` | `PI-2101A` | suction_pressure | kg/cm2(g) | 펌프 흡입압력 | 1 | — / 4.5 / 3.5 | — / 9 / — |
| `P-2101` | `PI-2101B` | discharge_pressure | kg/cm2(g) | 펌프 토출압력 | 1 | — / 170 / — | — / 198 / 205 |
| `P-2101` | `FI-2101` | flow | m3/h | 펌프 토출 유량 | 1 | — / 130 / 100 | — / 215 / — |
| `P-2101` | `II-2101` | current | A | 모터 전류 | 1 | — | — / 195 / 210 |
| `C-2201` | `VI-2201A` | vibration | mm/s RMS | 크랭크 프레임 진동 | 1 | — | 8 / 11 / 18 |
| `C-2201` | `VI-2201B` | vibration | mm/s RMS | 실린더 진동 | 1 | — | 8.5 / 11 / 18 |
| `C-2201` | `TI-2201A` | bearing_temp | DEG C | 메인 베어링 온도 | 10 | — | 85 / 95 / 105 |
| `C-2201` | `TI-2201B` | suction_temp | DEG C | 흡입 온도 | 10 | — | — / 55 / — |
| `C-2201` | `TI-2201C` | discharge_temp | DEG C | 토출 온도 | 10 | — | 135 / 145 / 160 |
| `C-2201` | `PI-2201A` | suction_pressure | kg/cm2(g) | 흡입압력 | 1 | — / 19 / 16 | — / 28 / — |
| `C-2201` | `PI-2201B` | discharge_pressure | kg/cm2(g) | 토출압력 | 1 | — / 150 / — | — / 178 / 185 |
| `C-2201` | `FI-2201` | flow | Nm3/h | 토출 가스 유량 | 1 | — / 26000 / 20000 | — / 46000 / — |
| `C-2201` | `SI-2201` | speed | rpm | 압축기 회전속도 | 1 | — / 300 / — | — / 360 / 375 |
| `C-2301` | `VI-2301A` | shaft_vibration | um pk-pk | 구동측 축 진동 (X) | 1 | — | 22 / 28 / 38 |
| `C-2301` | `VI-2301B` | shaft_vibration | um pk-pk | 구동측 축 진동 (Y) | 1 | — | 22 / 28 / 38 |
| `C-2301` | `VI-2301C` | shaft_vibration | um pk-pk | 반구동측 축 진동 (X) | 1 | — | 22 / 28 / 38 |
| `C-2301` | `VI-2301D` | shaft_vibration | um pk-pk | 반구동측 축 진동 (Y) | 1 | — | 22 / 28 / 38 |
| `C-2301` | `TI-2301A` | bearing_temp | DEG C | 구동측 저널 베어링 패드 온도 | 10 | — | 100 / 115 / 125 |
| `C-2301` | `TI-2301B` | bearing_temp | DEG C | 반구동측 저널 베어링 패드 온도 | 10 | — | 100 / 115 / 125 |
| `C-2301` | `ZI-2301` | axial_position | mm | 축방향 위치 | 1 | — / -0.3 / -0.5 | 0.2 / 0.3 / 0.5 |
| `C-2301` | `PI-2301A` | suction_pressure | kg/cm2(g) | 흡입압력 | 1 | — / 115 / — | — / 140 / — |
| `C-2301` | `PI-2301B` | discharge_pressure | kg/cm2(g) | 토출압력 | 1 | — / 128 / — | — / 155 / — |
| `C-2301` | `TI-2301C` | suction_temp | DEG C | 흡입 온도 | 10 | — | — / 70 / — |
| `C-2301` | `TI-2301D` | discharge_temp | DEG C | 토출 온도 | 10 | — | 95 / 105 / 115 |
| `C-2301` | `FI-2301` | flow | Nm3/h | 순환 가스 유량 | 1 | — / 215000 / 190000 | — / 330000 / — |
| `C-2301` | `SI-2301` | speed | rpm | 회전속도 | 1 | — / 4600 / — | — / 5517 / 5700 |
| `FN-2401` | `VI-2401A` | vibration | mm/s RMS | 팬 구동측 진동 | 1 | — | 3.5 / 4.5 / 7.1 |
| `FN-2401` | `VI-2401B` | vibration | mm/s RMS | 팬 반구동측 진동 | 1 | — | 3.5 / 4.5 / 7.1 |
| `FN-2401` | `TI-2401A` | bearing_temp | DEG C | 팬 구동측 베어링 온도 | 10 | — | 75 / 85 / 95 |
| `FN-2401` | `TI-2401B` | bearing_temp | DEG C | 팬 반구동측 베어링 온도 | 10 | — | 75 / 85 / 95 |
| `FN-2401` | `II-2401` | current | A | 모터 전류 | 1 | — | — / 98 / 108 |
| `FN-2401` | `SI-2401` | speed | rpm | 팬 회전속도 | 1 | — / 1600 / — | — / 1830 / 1860 |
| `FN-2401` | `FI-2401` | flow | Nm3/h | 공기 유량 | 1 | — / 45000 / 38000 | — / 72000 / — |
| `FN-2401` | `PDI-2401` | differential_pressure | mmH2O | 팬 차압 | 1 | — / 140 / — | — / 230 / — |
| `P-2501` | `VI-2501` | vibration | mm/s RMS | 펌프 진동 | 1 | — | 3.5 / 4.5 / 7.1 |
| `P-2501` | `TI-2501A` | bearing_temp | DEG C | 펌프 베어링 온도 | 10 | — | 80 / 90 / 100 |
| `P-2501` | `PI-2501` | oil_pressure | kg/cm2(g) | 윤활유 토출압력 | 1 | — / 6 / 5 | — / 9.5 / — |
| `P-2501` | `TI-2501B` | oil_temp | DEG C | 윤활유 온도 | 10 | — | 58 / 65 / 75 |
| `P-2501` | `FI-2501` | flow | m3/h | 윤활유 유량 | 1 | — / 30 / 24 | — / 52 / — |
| `P-2501` | `II-2501` | current | A | 모터 전류 | 1 | — | — / 30 / 34 |
| `P-2601` | `VI-2601A` | vibration | mm/s RMS | 펌프 구동측 진동 | 1 | — | 3.5 / 4.5 / 7.1 |
| `P-2601` | `VI-2601B` | vibration | mm/s RMS | 펌프 반구동측 진동 | 1 | — | 3.5 / 4.5 / 7.1 |
| `P-2601` | `TI-2601A` | bearing_temp | DEG C | 펌프 구동측 베어링 온도 | 10 | — | 80 / 90 / 100 |
| `P-2601` | `TI-2601B` | bearing_temp | DEG C | 펌프 반구동측 베어링 온도 | 10 | — | 80 / 90 / 100 |
| `P-2601` | `PI-2601A` | suction_pressure | kg/cm2(g) | 펌프 흡입압력 | 1 | — / 2.5 / 1.5 | — / 6 / — |
| `P-2601` | `PI-2601B` | discharge_pressure | kg/cm2(g) | 펌프 토출압력 | 1 | — / 7.5 / 6 | — / 12 / — |
| `P-2601` | `FI-2601` | flow | m3/h | 환류 유량 | 1 | — / 55 / 40 | — / 120 / — |
| `P-2601` | `II-2601` | current | A | 모터 전류 | 1 | — | — / 56 / 62 |
| `P-2701` | `VI-2701A` | vibration | mm/s RMS | 펌프 구동측 진동 | 1 | — | 3.5 / 4.5 / 7.1 |
| `P-2701` | `VI-2701B` | vibration | mm/s RMS | 펌프 반구동측 진동 | 1 | — | 3.5 / 4.5 / 7.1 |
| `P-2701` | `TI-2701A` | bearing_temp | DEG C | 펌프 구동측 베어링 온도 | 10 | — | 82 / 90 / 100 |
| `P-2701` | `TI-2701B` | bearing_temp | DEG C | 펌프 반구동측 베어링 온도 | 10 | — | 82 / 90 / 100 |
| `P-2701` | `TI-2701C` | process_temp | DEG C | 취급 유체 온도 | 10 | — | — / 330 / 345 |
| `P-2701` | `PI-2701A` | suction_pressure | kg/cm2(g) | 펌프 흡입압력 | 1 | — / 2.2 / 1.4 | — / 5.5 / — |
| `P-2701` | `PI-2701B` | discharge_pressure | kg/cm2(g) | 펌프 토출압력 | 1 | — / 26 / 22 | — / 38 / — |
| `P-2701` | `FI-2701` | flow | m3/h | 순환 유량 | 1 | — / 95 / 75 | — / 170 / — |
| `P-2701` | `II-2701` | current | A | 모터 전류 | 1 | — | — / 118 / 128 |

</details>

### 3.4 RHDS (상압 잔사유 수첨탈황) — `rhds` (`PL-RHDS`)

설비 6대 · 태그 43개

| 설비 | 이름 | 유형 | 공정 그룹 / 단위 | 태그 수 |
| --- | --- | --- | --- | ---: |
| `P-3101` | 고압 원료 공급 펌프 (High Pressure Feed Pump) | PM 펌프 | 원료 공급 / 잔사유 가압·공급 | 6 |
| `C-3102` | 보충 수소 압축기 (Make-up Hydrogen Compressor) | CC 압축기 | 수소 공급 / 보충 수소 압축 | 8 |
| `C-3101` | 순환가스 압축기 (Recycle Gas Compressor) | CC 압축기 | 수첨탈황 반응 / 수소 순환 | 11 |
| `F-3101` | 가열로 강제통풍 팬 (FD Fan) | FN 팬 | 반응 가열 / 연소공기 공급 | 6 |
| `P-3102` | 고온 잔사유 이송 펌프 (Hot Residue Pump) | PM 펌프 | 고온 유체 처리 / 잔사유 이송 | 7 |
| `P-3103` | 분별탑 환류 펌프 (Fractionator Reflux Pump) | PM 펌프 | 생성물 분별 / 환류·순환 | 5 |

<details><summary>태그 목록 (한계값: 경고 / 알람 / 정지)</summary>

| 설비 | 태그 | 신호 | 단위 | 설명 | 주기(s) | 하한 | 상한 |
| --- | --- | --- | --- | --- | ---: | --- | --- |
| `P-3101` | `VI-A-P3101` | vibration | mm/s RMS | 구동측 수평 케이싱 진동 | 1 | — | 4.5 / 7.1 / 11 |
| `P-3101` | `TI-B-P3101` | bearing_temp | DEG C | 구동측 베어링 온도 | 10 | — | 85 / 95 / 105 |
| `P-3101` | `PI-S-P3101` | suction_pressure | bar(g) | 흡입 압력 | 1 | 3.5 / 2.8 / 2.24 | — |
| `P-3101` | `PI-D-P3101` | discharge_pressure | bar(g) | 토출 압력 | 1 | — | 170 / 178 / 187 |
| `P-3101` | `FI-P3101` | flow | m3/h | 원료 유량 | 1 | 160 / 138 / 110 | — |
| `P-3101` | `II-P3101` | current | A | 모터 전류 | 1 | — | 210 / 223 / 240 |
| `C-3102` | `VI-A-C3102` | vibration | mm/s RMS | 프레임 케이싱 진동 | 1 | — | 6 / 7.1 / 11 |
| `C-3102` | `TI-B-C3102` | bearing_temp | DEG C | 프레임 베어링 온도 | 10 | — | 85 / 95 / 105 |
| `C-3102` | `PI-S-C3102` | suction_pressure | bar(g) | 첫 단 흡입 압력 | 1 | 20 / 16 / 12.8 | — |
| `C-3102` | `PI-D-C3102` | discharge_pressure | bar(g) | 최종 단 토출 압력 | 1 | — | 175 / 184 / 193 |
| `C-3102` | `TI-S-C3102` | suction_temp | DEG C | 첫 단 흡입 가스 온도 | 10 | — | 50 / 60 / 70 |
| `C-3102` | `TI-D-C3102` | discharge_temp | DEG C | 최종 단 토출 가스 온도 | 10 | — | 145 / 155 / 165 |
| `C-3102` | `FI-C3102` | flow | Nm3/h | 보충 수소 표준 유량 | 1 | 9000 / 7740 / 6190 | — |
| `C-3102` | `SI-C3102` | speed | rpm | 크랭크축 회전속도 | 1 | 570 / 547 / — | 610 / 634 / 659 |
| `C-3101` | `XI-X-C3101` | shaft_vibration | um pk-pk | 구동측 X방향 축 상대 진동 | 1 | — | 75 / 95.2 / 130 |
| `C-3101` | `TI-B-C3101` | bearing_temp | DEG C | 저널 베어링 금속 온도 | 10 | — | 95 / 105 / 115 |
| `C-3101` | `ZI-C3101` | axial_position | mm | 축방향 위치·기준점 대비 | 1 | -0.35 / -0.525 / -0.875 | 0.35 / 0.525 / 0.875 |
| `C-3101` | `PI-S-C3101` | suction_pressure | bar(g) | 흡입 압력 | 1 | 125 / 100 / 80 | — |
| `C-3101` | `PI-D-C3101` | discharge_pressure | bar(g) | 토출 압력 | 1 | — | 170 / 178 / 187 |
| `C-3101` | `TI-S-C3101` | suction_temp | DEG C | 흡입 가스 온도 | 10 | — | 60 / 70 / 80 |
| `C-3101` | `TI-D-C3101` | discharge_temp | DEG C | 토출 가스 온도 | 10 | — | 95 / 105 / 115 |
| `C-3101` | `FI-C3101` | flow | Nm3/h | 순환가스 표준 유량 | 1 | 110000 / 94600 / 75700 | — |
| `C-3101` | `SI-C3101` | speed | rpm | 압축기 회전속도 | 1 | 8500 / 8160 / — | 10000 / 10400 / 10800 |
| `C-3101` | `PI-LO-C3101` | oil_pressure | bar(g) | 윤활유 헤더 압력 | 1 | 2 / 1.6 / 1.33 | — |
| `C-3101` | `TI-LO-C3101` | oil_temp | DEG C | 윤활유 공급 온도 | 10 | — | 55 / 62 / 72 |
| `F-3101` | `VI-A-F3101` | vibration | mm/s RMS | 팬 구동측 케이싱 진동 | 1 | — | 4.5 / 7.1 / 11 |
| `F-3101` | `TI-B-F3101` | bearing_temp | DEG C | 팬 구동측 베어링 온도 | 10 | — | 80 / 90 / 100 |
| `F-3101` | `SI-F3101` | speed | rpm | 팬 회전속도 | 1 | 1150 / 1100 / — | 1550 / 1610 / 1670 |
| `F-3101` | `II-F3101` | current | A | 모터 전류 | 1 | — | 80 / 84.8 / 91.6 |
| `F-3101` | `FI-F3101` | flow | m3/h | 연소공기 실제 체적 유량 | 1 | 35000 / 30100 / 24100 | — |
| `F-3101` | `PDI-F3101` | differential_pressure | Pa | 팬 입출구 차압 | 1 | 1400 / 1200 / — | — |
| `P-3102` | `VI-A-P3102` | vibration | mm/s RMS | 구동측 케이싱 진동 | 1 | — | 4.5 / 7.1 / 11 |
| `P-3102` | `TI-B-P3102` | bearing_temp | DEG C | 구동측 베어링 온도 | 10 | — | 90 / 100 / 110 |
| `P-3102` | `TI-F-P3102` | process_temp | DEG C | 흡입 유체 온도 | 10 | — | 310 / 320 / 335 |
| `P-3102` | `PI-S-P3102` | suction_pressure | bar(g) | 흡입 압력 | 1 | 3 / 2.4 / 1.92 | — |
| `P-3102` | `PI-D-P3102` | discharge_pressure | bar(g) | 토출 압력 | 1 | — | 25 / 26.2 / 27.5 |
| `P-3102` | `FI-P3102` | flow | m3/h | 잔사유 유량 | 1 | 140 / 120 / 96.3 | — |
| `P-3102` | `II-P3102` | current | A | 모터 전류 | 1 | — | 145 / 154 / 166 |
| `P-3103` | `VI-A-P3103` | vibration | mm/s RMS | 구동측 케이싱 진동 | 1 | — | 4.5 / 7.1 / 11 |
| `P-3103` | `TI-B-P3103` | bearing_temp | DEG C | 구동측 베어링 온도 | 10 | — | 80 / 90 / 100 |
| `P-3103` | `PI-S-P3103` | suction_pressure | bar(g) | 흡입 압력 | 1 | 2 / 1.6 / 1.28 | — |
| `P-3103` | `PI-D-P3103` | discharge_pressure | bar(g) | 토출 압력 | 1 | — | 15 / 15.8 / 16.5 |
| `P-3103` | `FI-P3103` | flow | m3/h | 환류액 유량 | 1 | 65 / 55.9 / 44.7 | — |

</details>

### 3.5 VRDS (감압 잔사유 수첨탈황) — `vrds` (`PL-VRDS`)

설비 6대 · 태그 43개

| 설비 | 이름 | 유형 | 공정 그룹 / 단위 | 태그 수 |
| --- | --- | --- | --- | ---: |
| `P-4101` | 고압 원료 공급 펌프 (High Pressure Feed Pump) | PM 펌프 | 원료 공급 / 잔사유 가압·공급 | 6 |
| `C-4102` | 보충 수소 압축기 (Make-up Hydrogen Compressor) | CC 압축기 | 수소 공급 / 보충 수소 압축 | 8 |
| `C-4101` | 순환가스 압축기 (Recycle Gas Compressor) | CC 압축기 | 수첨탈황 반응 / 수소 순환 | 11 |
| `F-4101` | 가열로 강제통풍 팬 (FD Fan) | FN 팬 | 반응 가열 / 연소공기 공급 | 6 |
| `P-4102` | 고온 잔사유 이송 펌프 (Hot Residue Pump) | PM 펌프 | 고온 유체 처리 / 잔사유 이송 | 7 |
| `P-4103` | 분별탑 환류 펌프 (Fractionator Reflux Pump) | PM 펌프 | 생성물 분별 / 환류·순환 | 5 |

<details><summary>태그 목록 (한계값: 경고 / 알람 / 정지)</summary>

| 설비 | 태그 | 신호 | 단위 | 설명 | 주기(s) | 하한 | 상한 |
| --- | --- | --- | --- | --- | ---: | --- | --- |
| `P-4101` | `VI-A-P4101` | vibration | mm/s RMS | 구동측 수평 케이싱 진동 | 1 | — | 4.5 / 7.1 / 11 |
| `P-4101` | `TI-B-P4101` | bearing_temp | DEG C | 구동측 베어링 온도 | 10 | — | 85 / 95 / 105 |
| `P-4101` | `PI-S-P4101` | suction_pressure | bar(g) | 흡입 압력 | 1 | 3.5 / 2.8 / 2.24 | — |
| `P-4101` | `PI-D-P4101` | discharge_pressure | bar(g) | 토출 압력 | 1 | — | 170 / 178 / 187 |
| `P-4101` | `FI-P4101` | flow | m3/h | 원료 유량 | 1 | 160 / 138 / 110 | — |
| `P-4101` | `II-P4101` | current | A | 모터 전류 | 1 | — | 210 / 223 / 240 |
| `C-4102` | `VI-A-C4102` | vibration | mm/s RMS | 프레임 케이싱 진동 | 1 | — | 6 / 7.1 / 11 |
| `C-4102` | `TI-B-C4102` | bearing_temp | DEG C | 프레임 베어링 온도 | 10 | — | 85 / 95 / 105 |
| `C-4102` | `PI-S-C4102` | suction_pressure | bar(g) | 첫 단 흡입 압력 | 1 | 20 / 16 / 12.8 | — |
| `C-4102` | `PI-D-C4102` | discharge_pressure | bar(g) | 최종 단 토출 압력 | 1 | — | 175 / 184 / 193 |
| `C-4102` | `TI-S-C4102` | suction_temp | DEG C | 첫 단 흡입 가스 온도 | 10 | — | 50 / 60 / 70 |
| `C-4102` | `TI-D-C4102` | discharge_temp | DEG C | 최종 단 토출 가스 온도 | 10 | — | 145 / 155 / 165 |
| `C-4102` | `FI-C4102` | flow | Nm3/h | 보충 수소 표준 유량 | 1 | 9000 / 7740 / 6190 | — |
| `C-4102` | `SI-C4102` | speed | rpm | 크랭크축 회전속도 | 1 | 570 / 547 / — | 610 / 634 / 659 |
| `C-4101` | `XI-X-C4101` | shaft_vibration | um pk-pk | 구동측 X방향 축 상대 진동 | 1 | — | 75 / 95.2 / 130 |
| `C-4101` | `TI-B-C4101` | bearing_temp | DEG C | 저널 베어링 금속 온도 | 10 | — | 95 / 105 / 115 |
| `C-4101` | `ZI-C4101` | axial_position | mm | 축방향 위치·기준점 대비 | 1 | -0.35 / -0.525 / -0.875 | 0.35 / 0.525 / 0.875 |
| `C-4101` | `PI-S-C4101` | suction_pressure | bar(g) | 흡입 압력 | 1 | 125 / 100 / 80 | — |
| `C-4101` | `PI-D-C4101` | discharge_pressure | bar(g) | 토출 압력 | 1 | — | 170 / 178 / 187 |
| `C-4101` | `TI-S-C4101` | suction_temp | DEG C | 흡입 가스 온도 | 10 | — | 60 / 70 / 80 |
| `C-4101` | `TI-D-C4101` | discharge_temp | DEG C | 토출 가스 온도 | 10 | — | 95 / 105 / 115 |
| `C-4101` | `FI-C4101` | flow | Nm3/h | 순환가스 표준 유량 | 1 | 110000 / 94600 / 75700 | — |
| `C-4101` | `SI-C4101` | speed | rpm | 압축기 회전속도 | 1 | 8500 / 8160 / — | 10000 / 10400 / 10800 |
| `C-4101` | `PI-LO-C4101` | oil_pressure | bar(g) | 윤활유 헤더 압력 | 1 | 2 / 1.6 / 1.33 | — |
| `C-4101` | `TI-LO-C4101` | oil_temp | DEG C | 윤활유 공급 온도 | 10 | — | 55 / 62 / 72 |
| `F-4101` | `VI-A-F4101` | vibration | mm/s RMS | 팬 구동측 케이싱 진동 | 1 | — | 4.5 / 7.1 / 11 |
| `F-4101` | `TI-B-F4101` | bearing_temp | DEG C | 팬 구동측 베어링 온도 | 10 | — | 80 / 90 / 100 |
| `F-4101` | `SI-F4101` | speed | rpm | 팬 회전속도 | 1 | 1150 / 1100 / — | 1550 / 1610 / 1670 |
| `F-4101` | `II-F4101` | current | A | 모터 전류 | 1 | — | 80 / 84.8 / 91.6 |
| `F-4101` | `FI-F4101` | flow | m3/h | 연소공기 실제 체적 유량 | 1 | 35000 / 30100 / 24100 | — |
| `F-4101` | `PDI-F4101` | differential_pressure | Pa | 팬 입출구 차압 | 1 | 1400 / 1200 / — | — |
| `P-4102` | `VI-A-P4102` | vibration | mm/s RMS | 구동측 케이싱 진동 | 1 | — | 4.5 / 7.1 / 11 |
| `P-4102` | `TI-B-P4102` | bearing_temp | DEG C | 구동측 베어링 온도 | 10 | — | 90 / 100 / 110 |
| `P-4102` | `TI-F-P4102` | process_temp | DEG C | 흡입 유체 온도 | 10 | — | 310 / 320 / 335 |
| `P-4102` | `PI-S-P4102` | suction_pressure | bar(g) | 흡입 압력 | 1 | 3 / 2.4 / 1.92 | — |
| `P-4102` | `PI-D-P4102` | discharge_pressure | bar(g) | 토출 압력 | 1 | — | 25 / 26.2 / 27.5 |
| `P-4102` | `FI-P4102` | flow | m3/h | 잔사유 유량 | 1 | 140 / 120 / 96.3 | — |
| `P-4102` | `II-P4102` | current | A | 모터 전류 | 1 | — | 145 / 154 / 166 |
| `P-4103` | `VI-A-P4103` | vibration | mm/s RMS | 구동측 케이싱 진동 | 1 | — | 4.5 / 7.1 / 11 |
| `P-4103` | `TI-B-P4103` | bearing_temp | DEG C | 구동측 베어링 온도 | 10 | — | 80 / 90 / 100 |
| `P-4103` | `PI-S-P4103` | suction_pressure | bar(g) | 흡입 압력 | 1 | 2 / 1.6 / 1.28 | — |
| `P-4103` | `PI-D-P4103` | discharge_pressure | bar(g) | 토출 압력 | 1 | — | 15 / 15.8 / 16.5 |
| `P-4103` | `FI-P4103` | flow | m3/h | 환류액 유량 | 1 | 65 / 55.9 / 44.7 | — |

</details>

## 4. 표준 신호 × 공장 (태그 수)

| signal | rfcc | cdu | hou | rhds | vrds | 합계 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| vibration | 8 | 14 | 11 | 5 | 5 | 43 |
| shaft_vibration | 8 | — | 4 | 1 | 1 | 14 |
| bearing_temp | 12 | 7 | 12 | 6 | 6 | 43 |
| suction_temp | 1 | — | 2 | 2 | 2 | 7 |
| discharge_temp | 1 | — | 2 | 2 | 2 | 7 |
| oil_temp | 2 | — | 1 | 1 | 1 | 5 |
| process_temp | — | 2 | 1 | 1 | 1 | 5 |
| suction_pressure | 11 | 4 | 5 | 5 | 5 | 30 |
| discharge_pressure | 11 | 6 | 5 | 5 | 5 | 32 |
| oil_pressure | 2 | — | 1 | 1 | 1 | 5 |
| steam_pressure | 1 | — | — | — | — | 1 |
| differential_pressure | — | 1 | 1 | 1 | 1 | 4 |
| flow | 10 | 7 | 7 | 6 | 6 | 36 |
| current | 8 | 4 | 5 | 3 | 3 | 23 |
| speed | 4 | 1 | 3 | 3 | 3 | 14 |
| axial_position | 2 | — | 1 | 1 | 1 | 5 |

## 5. 테이블 구성 전에 정할 것

정리하면서 보인 점이다. 1–3 은 공장별 스키마로 나누기로 하면서 해소됐다. PostgreSQL 쪽에서 정할 것은 6.5 에 있다.

1. ~~공장을 한 DB 로 합칠지~~ → 공장별 스키마. 참고로 `equip_id`·`unit_id` 는 다섯 공장 사이에 겹치지 않고, `equipment` 에 `plant_id` 가 없어 공장은 `unit → unit_group → plant` 로 찾는다 (스키마 안에서는 공장이 하나뿐이라 문제없다).
2. **`tag_std` 가 공장마다 다르다** → 스키마별로 따로 두므로 충돌 없음. 같은 `signal` 인데 접두어·단위 표기가 다르다. 예) `shaft_vibration` 접두어 VI / XI / VX, `suction_pressure` 단위 bar(g) / kg/cm2(g), `bearing_temp` 단위 DEG C / degC. 공장을 가로지르는 조회에서는 `equipment_tag.unit` 을 기준으로 본다.
3. **`equipment_type` 도 다르다** → 스키마별로 따로 두므로 충돌 없음. `CC` 가 RFCC 에서는 “원심 압축기”, 나머지에서는 “압축기”.
4. **비어 있는 테이블**: `equipment_alias`, `maintenance_history`, `failure_history` 는 다섯 공장 모두 0행이다 (별칭·정비·고장 자료가 구성안·시나리오에 없음). `equipment.grade`, `equipment_tag.location` 도 비어 있다.
5. **한계값**: RFCC 81개 태그는 통계 경고값만 있다 (`delay_samples` 2). RHDS·VRDS 는 경고값이 자료 그대로이고, 알람·정지값은 CDU·HOU 의 같은 신호 간격을 적용해 넣었다 (규칙은 각 태그 `limit_basis`). 나머지 공장의 `delay_samples` 는 기본값 1.
6. **원시 값 크기**: 다섯 공장 합계 하루 약 1,846만 행 (행당 약 180 B → 하루 약 3.3 GB). 보관 기간이 1일이라 원시 값은 이 정도에서 고정되고, 집계가 계속 쌓인다.

## 6. PostgreSQL 이상 데이터 DB (결정)

수집 서버의 SQLite 는 지금처럼 작업용으로 둔다 (원시 값 1일 보관, 집계·알람 배치). PostgreSQL 에는 **이상이 탐지된 것만** 옮기고 **지우지 않고** 쌓는다.

### 6.1 구성

- **DB 하나, 공장별 스키마 다섯 개**: 스키마 이름은 `config/plants.yaml` 의 `key` 그대로 → `rfcc`, `cdu`, `hou`, `rhds`, `vrds`. 공장 사이에 공유하는 테이블은 없다. 공장마다 `search_path` 를 그 스키마로 잡아 테이블 이름은 지금 그대로 쓴다.
- **이상 판정** 두 가지:
  1. **한계값 초과**: 10분 배치가 만드는 `alarm_detail` 그대로 (10분 최대값↔상한, 최소값↔하한, `delay_samples`).
  2. **품질 이상**: 원시 값 품질이 `Bad` / `ScanOff` 인 구간. `Uncertain` 은 값이 있으므로 이상으로 보지 않는다.
- **저장 범위**: 이상 1건당 한 행 + 이상 구간 앞뒤 원시 값.
- RFCC 의 한계값 초과는 통계 경고뿐이라 모두 Soft(Low) 다. RHDS·VRDS 는 알람·정지값이 생겨 Medium·High 도 나온다.

### 6.2 스키마별 테이블

| 구분 | 테이블 | 키 | 내용 | SQLite 에서 |
| --- | --- | --- | --- | --- |
| 마스터 | `schema_meta`, `plant`, `unit_group`, `unit`, `equipment_type`, `equipment`, `equipment_alias`, `tag_std`, `equipment_tag` | 2.1 과 같음 | 이상 행을 해석할 기준 (계층·한계값·단위) | 구성안에서 서버 시작 때 upsert (지금과 같은 경로) |
| 이력 | `maintenance_history`, `failure_history` | 2.2 와 같음 | 정비·고장 이력 | 그대로 |
| 이상 | `alarm_detail` | `alarm_id` | 한계값 초과 1건 = 1행 | 그대로 옮김. `alarm_id` 는 SQLite 값을 PK 로 써서 해제(`cleared_at`) 때 같은 행을 갱신 |
| 이상 | `quality_event` **(신규)** | `(equip_id, tag_id, start_ts)` | 품질 이상 1건 = 1행 | SQLite 에도 같은 테이블을 두고 `alarm_detail` 처럼 열고 닫는다 |
| 이상 | `anomaly_raw` **(신규)** | `(equip_id, tag_id, ts)` | 이상 구간 앞뒤 원시 값 | `ts_raw` 에서 잘라 옮김 |

**옮기지 않는 테이블**: `ts_raw`(전체), `tag_current`, `ts_10m`, `ts_1h`, `ingest_batch`, `job_state` — 수집 서버 작업용으로 SQLite 에만 둔다.

`quality_event` 컬럼:

| 컬럼 | 타입 | 설명 |
| --- | --- | --- |
| `equip_id`, `tag_id` | text | PK |
| `start_ts` | timestamptz | PK, 첫 이상 샘플 시각 |
| `end_ts` | timestamptz | 마지막 이상 샘플 시각. 진행 중이면 NULL |
| `quality` | text | `Bad` / `ScanOff`. 도중에 바뀌면 다른 건으로 나눈다 |
| `sample_count` | integer | 이상 샘플 수 |

`anomaly_raw` 컬럼:

| 컬럼 | 타입 | 설명 |
| --- | --- | --- |
| `equip_id`, `tag_id` | text | PK |
| `ts` | timestamptz | PK, 측정 시각 |
| `value` | double precision | 측정값 (NULL 가능) |
| `quality` | text | Good / Uncertain / Bad / ScanOff |

- 원시 값은 이상 건에 매달지 않고 측정점 단위로 둔다. 창이 겹쳐도 PK 로 한 번만 들어가고 (`ON CONFLICT DO NOTHING`), 이상 건과는 `equip_id` + 시각 범위로 잇는다. 이 조회용으로 `(equip_id, ts)` 인덱스를 둔다.
- **창 기본안**: 같은 설비의 모든 태그, `발생 30분 전 ~ 해제 30분 후`. 1초 태그 6개 + 10초 태그 2개 설비면 1시간에 약 2.2만 행 (약 4 MB).

### 6.3 옮기는 시점

- 10분 배치 끝에 새로 생기거나 해제된 `alarm_detail`·`quality_event` 를 PostgreSQL 에 upsert 한다.
- 원시 값 창은 그때까지 모인 만큼 넣고 다음 배치에서 이어 넣는다 (PK 로 중복 무시). 진행 중인 이상도 10분마다 옮기므로 SQLite 원시 값 보관 1일 안에 빠짐없이 넘어간다.
- 옮긴 위치는 SQLite `job_state` 에 이름 하나(`pg_export`)를 더해 기록한다.

### 6.4 SQLite → PostgreSQL 타입·구문

| 지금 (SQLite) | PostgreSQL | 해당 컬럼·위치 |
| --- | --- | --- |
| `TEXT` (UTC ISO 시각) | `timestamptz` | `ts`, `start_ts`, `end_ts`, `occurred_at`, `cleared_at` |
| `REAL` | `double precision` | `value`, 한계값 여섯 칸, `limit_value`, `actual_value`, `exceed_pct` |
| `INTEGER` | `integer` | `config_version`, `grade`, `delay_samples`, `interval_sec`, `decimals`, `sample_count`, `version` |
| `TEXT` (그 밖) | `text` | ID·이름·설명·`quality` 등 |
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `bigint` (SQLite 값 그대로, IDENTITY 아님) | `alarm_detail.alarm_id` |
| `INSERT OR REPLACE` | `INSERT … ON CONFLICT (PK) DO UPDATE` | 마스터 적재, `alarm_detail`·`quality_event` 갱신 |

### 6.5 정할 것

1. **원시 값 창 길이**: 기본안 앞뒤 30분. 알람은 10분 구간 단위로 잡히므로 10분보다 짧게 잡을 이유는 없다.
2. **창에 넣을 태그**: 기본안은 같은 설비 전체 (진동이 오르면 같은 설비의 압력·유량이 같이 움직인다). RFCC 의 구동 짝(`B-1201`↔`E-1202`, `C-1401`↔`T-1401`)까지 넣을지.
3. **외래키**: 지우는 일이 없고 양이 작으므로 마스터끼리 + 이상 테이블 → `equipment_tag` 까지 거는 안을 권한다.
4. **공장을 가로지르는 조회**: 필요해지면 스키마별 이상 테이블을 `UNION ALL` 하는 뷰를 따로 둔다.
