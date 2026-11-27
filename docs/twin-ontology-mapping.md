# 플랜트 물리 회로 ↔ 온톨로지·3D 매핑 (D1)

작성 기준일 2026-11-24 · `scripts/twin_ontology_mapping.py` 생성. AI 편집 정리본이며 팀 승인 문서가 아니다. 키는 원본 그대로 쓴다: 설비 `plant_id/equip_id`, 태그 `plant_id/equip_id/tag_code`. 물리 파라미터는 synthetic assumption 이다.

| 플랜트 | 대상 Unit | 회로 | 설비 | 물리 태그 | 제외 설비 |
| --- | --- | --- | --- | --- | --- |
| CDU | UNIT-CDU-01, UNIT-CDU-07 | `coupled:P-8101A` P-8101A → DS → HX → HT → AT → P-8401A | P-8101A, P-8101B, P-8401A | 11 | P-8102A, P-8301A, P-8302A |
| CDU | UNIT-CDU-06 | `single:P-8303A` AT → P-8303A → virtual:residue-boundary | P-8303A | 3 |  |
| CDU | UNIT-CDU-03 | `single:F-8201` virtual:ambient-air → F-8201 → HT | F-8201 | 3 |  |
| HOU | UNIT-HOU-01, UNIT-HOU-07 | `coupled:P-2101` P-2101 → P-2101-S2 → MIX → HX → FURN → REACT → HX → SEP → STRIP → FRAC → P-2701 | P-2101, P-2101-S2, P-2701 | 12 | C-2201, C-2301, FN-2401, GB-2301, M-2201, M-2301, P-2501, P-2601 |
| RFCC | UNIT-RFCC-01, UNIT-RFCC-04 | `coupled:P-1101A` P-1101A → RX → FR → P-1303A | P-1101A, P-1101B, P-1303A | 12 | B-1201, C-1401, E-1202, P-1301A, P-1302A, P-1402A, P-1403A, P-1501A, P-1502A, T-1401 |
| RHDS | UNIT-RHDS-01, UNIT-RHDS-05 | `coupled:P-3101` P-3101 → P-3101-S2 → MX → HX → HT → RX → HX → SP → FR → P-3102 | P-3101, P-3101-S2, P-3102 | 12 | C-3101, C-3102, F-3101, GB-3101, M-3101, M-3102, P-3103 |
| VRDS | UNIT-VRDS-01, UNIT-VRDS-05 | `coupled:P-4101` P-4101 → P-4101-S2 → MX → HX → HT → RX → HX → SP → FR → P-4102 | P-4101, P-4101-S2, P-4102 | 12 | C-4101, C-4102, F-4101, GB-4101, M-4101, M-4102, P-4103 |

모든 압력은 내부 계산에서 절대압 Pa, 유량 m³/s 다. 가상 요소는 `virtual:` 키로만 쓰고 온톨로지·센서로 만들지 않는다.

## CDU (CDU)

| 설비 키 | 태그 키 | 물리량 | 단위 (YAML·SQL·CSV·3D) | SI 변환 | 주기 s (YAML·CSV) | 회로·역할 |
| --- | --- | --- | --- | --- | --- | --- |
| `CDU_P-8101A` | `CDU_P-8101A/FI-8101A` | 체적 유량 | m3/h · m3/h · m3/h · m3/h | m³/s = v/3,600 | 1 · 1 | `coupled:P-8101A` source |
| `CDU_P-8101A` | `CDU_P-8101A/II-8101A` | 모터 전류(축동력 비례 대용) | A · A · A · A | 축동력 비 × 정상 전류 | 1 · 1 | `coupled:P-8101A` source |
| `CDU_P-8101A` | `CDU_P-8101A/PI-8101A` | 흡입 정압(게이지) | kg/cm2(g) · kg/cm2(g) · kg/cm2(g) · kg/cm2(g) | Pa(abs) = v·98,066.5 + 101,325 | 1 · 1 | `coupled:P-8101A` source |
| `CDU_P-8101A` | `CDU_P-8101A/PI-8101B` | 토출 정압(게이지) | kg/cm2(g) · kg/cm2(g) · kg/cm2(g) · kg/cm2(g) | Pa(abs) = v·98,066.5 + 101,325 | 1 · 1 | `coupled:P-8101A` source |
| `CDU_P-8101B` | `CDU_P-8101B/FI-P8101B-A` | 체적 유량 | m3/h · m3/h · m3/h · m3/h | m³/s = v/3,600 | 1 · 1 | `coupled:P-8101A` source |
| `CDU_P-8101B` | `CDU_P-8101B/II-P8101B-A` | 모터 전류(축동력 비례 대용) | A · A · A · A | 축동력 비 × 정상 전류 | 1 · 1 | `coupled:P-8101A` source |
| `CDU_P-8101B` | `CDU_P-8101B/PI-P8101B-A` | 흡입 정압(게이지) | kg/cm2(g) · kg/cm2(g) · kg/cm2(g) · kg/cm2(g) | Pa(abs) = v·98,066.5 + 101,325 | 1 · 1 | `coupled:P-8101A` source |
| `CDU_P-8101B` | `CDU_P-8101B/PI-P8101B-B` | 토출 정압(게이지) | kg/cm2(g) · kg/cm2(g) · kg/cm2(g) · kg/cm2(g) | Pa(abs) = v·98,066.5 + 101,325 | 1 · 1 | `coupled:P-8101A` source |
| `CDU_P-8401A` | `CDU_P-8401A/FI-8401A` | 체적 유량 | m3/h · m3/h · m3/h · m3/h | m³/s = v/3,600 | 1 · 1 | `coupled:P-8101A` downstream |
| `CDU_P-8401A` | `CDU_P-8401A/II-8401A` | 모터 전류(축동력 비례 대용) | A · A · A · A | 축동력 비 × 정상 전류 | 1 · 1 | `coupled:P-8101A` downstream |
| `CDU_P-8401A` | `CDU_P-8401A/PI-8401B` | 토출 정압(게이지) | kg/cm2(g) · kg/cm2(g) · kg/cm2(g) · kg/cm2(g) | Pa(abs) = v·98,066.5 + 101,325 | 1 · 1 | `coupled:P-8101A` downstream |

공정 관계 `coupled:P-8101A`: P-8101A→DS (`CDU/flow/1`) → DS→HX (`CDU/flow/2`) → HX→HT (`VRDS/flow/3`) → HT→AT (`CDU/flow/4`) → AT→P-8401A (`CDU/flow/5`)
가상 요소(온톨로지에 없는 계산 전용 키): virtual:cdu-process-equivalent, virtual:discharge-header, virtual:feed-valve, virtual:other-products-branch, virtual:other-products-pipe, virtual:product-branch, virtual:suction-boundary

| `CDU_P-8303A` | `CDU_P-8303A/FI-8303A` | 체적 유량 | m3/h · m3/h · m3/h · m3/h | m³/s = v/3,600 | 1 · 1 | `single:P-8303A` single |
| `CDU_P-8303A` | `CDU_P-8303A/PI-8303A` | 흡입 정압(게이지) | kg/cm2(g) · kg/cm2(g) · kg/cm2(g) · kg/cm2(g) | Pa(abs) = v·98,066.5 + 101,325 | 1 · 1 | `single:P-8303A` single |
| `CDU_P-8303A` | `CDU_P-8303A/PI-8303B` | 토출 정압(게이지) | kg/cm2(g) · kg/cm2(g) · kg/cm2(g) · kg/cm2(g) | Pa(abs) = v·98,066.5 + 101,325 | 1 · 1 | `single:P-8303A` single |

공정 관계 `single:P-8303A`: AT → P-8303A → virtual:residue-boundary (경계: AT·virtual:residue-boundary, 온톨로지 간선 `CDU/flow/14`)

| `CDU_F-8201` | `CDU_F-8201/FI-8201A` | 체적 유량 | Nm3/h · Nm3/h · Nm3/h · Nm3/h | m³/s = v·ρN/ρ/3,600 (ρN 1.2931 kg/Nm³) | 1 · 1 | `single:F-8201` single |
| `CDU_F-8201` | `CDU_F-8201/II-8201A` | 모터 전류(축동력 비례 대용) | A · A · A · A | 축동력 비 × 정상 전류 | 1 · 1 | `single:F-8201` single |
| `CDU_F-8201` | `CDU_F-8201/PDI-8201A` | 팬 전후 차압 | mmH2O · mmH2O · mmH2O · mmH2O | Pa = v·9.80665 | 1 · 1 | `single:F-8201` single |

공정 관계 `single:F-8201`: virtual:ambient-air → F-8201 → HT (경계: virtual:ambient-air·HT, 온톨로지 간선 `CDU/flow/7`)

제외 설비와 사유:

- `CDU_P-8102A`: 탈염 세정수 펌프: 흡입압 태그가 없고 탈염기(DS) 혼합·수위 경계를 계산하지 않아 독립 경계조건을 검증할 수 없습니다.
- `CDU_P-8301A`: 중간 순환환류 펌프: AT→HX→AT 순환 루프와 탑 내부 수력을 계산하지 않으며 전류 태그가 없습니다.
- `CDU_P-8302A`: 탑정 환류 펌프: DR→AT 환류 루프의 드럼 수위·탑 압력 경계를 계산하지 않으며 전류 태그가 없습니다.

## HOU (HOU)

| 설비 키 | 태그 키 | 물리량 | 단위 (YAML·SQL·CSV·3D) | SI 변환 | 주기 s (YAML·CSV) | 회로·역할 |
| --- | --- | --- | --- | --- | --- | --- |
| `HOU_P-2101` | `HOU_P-2101/FI-2101` | 체적 유량 | m3/h · m3/h · m3/h · m3/h | m³/s = v/3,600 | 1 · 1 | `coupled:P-2101` source |
| `HOU_P-2101` | `HOU_P-2101/II-2101` | 모터 전류(축동력 비례 대용) | A · A · A · A | 축동력 비 × 정상 전류 | 1 · 1 | `coupled:P-2101` source |
| `HOU_P-2101` | `HOU_P-2101/PI-2101A` | 흡입 정압(게이지) | kg/cm2(g) · kg/cm2(g) · kg/cm2(g) · kg/cm2(g) | Pa(abs) = v·98,066.5 + 101,325 | 1 · 1 | `coupled:P-2101` source |
| `HOU_P-2101` | `HOU_P-2101/PI-2101B` | 토출 정압(게이지) | kg/cm2(g) · kg/cm2(g) · kg/cm2(g) · kg/cm2(g) | Pa(abs) = v·98,066.5 + 101,325 | 1 · 1 | `coupled:P-2101` source |
| `HOU_P-2101-S2` | `HOU_P-2101-S2/FI-2101S2` | 체적 유량 | m3/h · m3/h · m3/h · m3/h | m³/s = v/3,600 | 1 · 1 | `coupled:P-2101` source |
| `HOU_P-2101-S2` | `HOU_P-2101-S2/II-2101S2` | 모터 전류(축동력 비례 대용) | A · A · A · A | 축동력 비 × 정상 전류 | 1 · 1 | `coupled:P-2101` source |
| `HOU_P-2101-S2` | `HOU_P-2101-S2/PI-2101S2A` | 흡입 정압(게이지) | kg/cm2(g) · kg/cm2(g) · kg/cm2(g) · kg/cm2(g) | Pa(abs) = v·98,066.5 + 101,325 | 1 · 1 | `coupled:P-2101` source |
| `HOU_P-2101-S2` | `HOU_P-2101-S2/PI-2101S2B` | 토출 정압(게이지) | kg/cm2(g) · kg/cm2(g) · kg/cm2(g) · kg/cm2(g) | Pa(abs) = v·98,066.5 + 101,325 | 1 · 1 | `coupled:P-2101` source |
| `HOU_P-2701` | `HOU_P-2701/FI-2701` | 체적 유량 | m3/h · m3/h · m3/h · m3/h | m³/s = v/3,600 | 1 · 1 | `coupled:P-2101` downstream |
| `HOU_P-2701` | `HOU_P-2701/II-2701` | 모터 전류(축동력 비례 대용) | A · A · A · A | 축동력 비 × 정상 전류 | 1 · 1 | `coupled:P-2101` downstream |
| `HOU_P-2701` | `HOU_P-2701/PI-2701A` | 흡입 정압(게이지) | kg/cm2(g) · kg/cm2(g) · kg/cm2(g) · kg/cm2(g) | Pa(abs) = v·98,066.5 + 101,325 | 1 · 1 | `coupled:P-2101` downstream |
| `HOU_P-2701` | `HOU_P-2701/PI-2701B` | 토출 정압(게이지) | kg/cm2(g) · kg/cm2(g) · kg/cm2(g) · kg/cm2(g) | Pa(abs) = v·98,066.5 + 101,325 | 1 · 1 | `coupled:P-2101` downstream |

공정 관계 `coupled:P-2101`: P-2101→P-2101-S2 (`HOU/flow/26`) → P-2101-S2→MIX (`HOU/flow/27`) → MIX→HX (`HOU/flow/2`) → HX→FURN (`HOU/flow/3`) → FURN→REACT (`HOU/flow/4`) → REACT→HX (`HOU/flow/5`) → HX→SEP (`HOU/flow/6`) → SEP→STRIP (`HOU/flow/7`) → STRIP→FRAC (`HOU/flow/10`) → FRAC→P-2701 (`HOU/flow/14`)
가상 요소(온톨로지에 없는 계산 전용 키): virtual:discharge-header, virtual:other-branch, virtual:other-equivalent, virtual:process-equivalent, virtual:product-branch, virtual:product-control-valve, virtual:suction-boundary

제외 설비와 사유:

- `HOU_C-2201`: 보충 수소 기상 압축기: 압축성·다단 열역학·서지 모델이 없어 단상 비압축 회로로 표현할 수 없습니다. 기존 경험 신호 모드만 있습니다.
- `HOU_C-2301`: 순환가스 기상 압축기: 압축성·다단 열역학·서지 모델이 없어 단상 비압축 회로로 표현할 수 없습니다. 기존 경험 신호 모드만 있습니다.
- `HOU_FN-2401`: 팬 물리 후보(유량·차압·전류·회전수 태그 있음): CDU F-8201 과 같은 팬 회로 형식으로 만들 수 있으나 이 플랜트의 연소 공기 경로 경계와 Python·Modelica 검증을 수행하지 않았습니다. 미검증이므로 주입하지 않습니다.
- `HOU_GB-2301`: 축계 구동 요소(전동기·기어박스·터빈)로 수력 회로 요소가 아닙니다. 축 전파는 기존 경험 신호입니다.
- `HOU_M-2201`: 축계 구동 요소(전동기·기어박스·터빈)로 수력 회로 요소가 아닙니다. 축 전파는 기존 경험 신호입니다.
- `HOU_M-2301`: 축계 구동 요소(전동기·기어박스·터빈)로 수력 회로 요소가 아닙니다. 축 전파는 기존 경험 신호입니다.
- `HOU_P-2501`: 압축기 윤활유 펌프: 흡입압 태그가 없고 윤활유 저장조·베어링 분배 회로 경계를 계산하지 않습니다.
- `HOU_P-2601`: 분별탑 환류 펌프: FRAC→P-2601→FRAC 환류 루프와 탑 내부 수력을 계산하지 않습니다.

## RFCC (RFCC)

| 설비 키 | 태그 키 | 물리량 | 단위 (YAML·SQL·CSV·3D) | SI 변환 | 주기 s (YAML·CSV) | 회로·역할 |
| --- | --- | --- | --- | --- | --- | --- |
| `RFCC_P-1101A` | `RFCC_P-1101A/FI-P-1101A` | 체적 유량 | m3/h · m3/h · m3/h · m3/h | m³/s = v/3,600 | 1 · 1 | `coupled:P-1101A` source |
| `RFCC_P-1101A` | `RFCC_P-1101A/II-P-1101A` | 모터 전류(축동력 비례 대용) | A · A · A · A | 축동력 비 × 정상 전류 | 1 · 1 | `coupled:P-1101A` source |
| `RFCC_P-1101A` | `RFCC_P-1101A/PD-P-1101A` | 토출 정압(게이지) | bar(g) · bar(g) · bar(g) · bar(g) | Pa(abs) = v·100,000 + 101,325 | 1 · 1 | `coupled:P-1101A` source |
| `RFCC_P-1101A` | `RFCC_P-1101A/PS-P-1101A` | 흡입 정압(게이지) | bar(g) · bar(g) · bar(g) · bar(g) | Pa(abs) = v·100,000 + 101,325 | 1 · 1 | `coupled:P-1101A` source |
| `RFCC_P-1101B` | `RFCC_P-1101B/FI-P-1101B` | 체적 유량 | m3/h · m3/h · m3/h · m3/h | m³/s = v/3,600 | 1 · 1 | `coupled:P-1101A` source |
| `RFCC_P-1101B` | `RFCC_P-1101B/II-P-1101B` | 모터 전류(축동력 비례 대용) | A · A · A · A | 축동력 비 × 정상 전류 | 1 · 1 | `coupled:P-1101A` source |
| `RFCC_P-1101B` | `RFCC_P-1101B/PD-P-1101B` | 토출 정압(게이지) | bar(g) · bar(g) · bar(g) · bar(g) | Pa(abs) = v·100,000 + 101,325 | 1 · 1 | `coupled:P-1101A` source |
| `RFCC_P-1101B` | `RFCC_P-1101B/PS-P-1101B` | 흡입 정압(게이지) | bar(g) · bar(g) · bar(g) · bar(g) | Pa(abs) = v·100,000 + 101,325 | 1 · 1 | `coupled:P-1101A` source |
| `RFCC_P-1303A` | `RFCC_P-1303A/FI-P-1303A` | 체적 유량 | m3/h · m3/h · m3/h · m3/h | m³/s = v/3,600 | 1 · 1 | `coupled:P-1101A` downstream |
| `RFCC_P-1303A` | `RFCC_P-1303A/II-P-1303A` | 모터 전류(축동력 비례 대용) | A · A · A · A | 축동력 비 × 정상 전류 | 1 · 1 | `coupled:P-1101A` downstream |
| `RFCC_P-1303A` | `RFCC_P-1303A/PD-P-1303A` | 토출 정압(게이지) | bar(g) · bar(g) · bar(g) · bar(g) | Pa(abs) = v·100,000 + 101,325 | 1 · 1 | `coupled:P-1101A` downstream |
| `RFCC_P-1303A` | `RFCC_P-1303A/PS-P-1303A` | 흡입 정압(게이지) | bar(g) · bar(g) · bar(g) · bar(g) | Pa(abs) = v·100,000 + 101,325 | 1 · 1 | `coupled:P-1101A` downstream |

공정 관계 `coupled:P-1101A`: P-1101A→RX (`RFCC/flow/1`) → RX→FR (`RFCC/flow/2`) → FR→P-1303A (`RFCC/flow/14`)
가상 요소(온톨로지에 없는 계산 전용 키): virtual:discharge-header, virtual:other-branch, virtual:other-equivalent, virtual:process-equivalent, virtual:product-branch, virtual:product-control-valve, virtual:suction-boundary

제외 설비와 사유:

- `RFCC_B-1201`: 주 공기 블로어(E-1202 축 구동): 압축성 공기·팽창기 축 결합 열역학 모델이 없습니다.
- `RFCC_C-1401`: 습가스 기상 압축기: 압축성·다단 열역학·서지 모델이 없어 단상 비압축 회로로 표현할 수 없습니다. 기존 경험 신호 모드만 있습니다.
- `RFCC_E-1202`: 동력 회수 팽창기: 배가스 팽창 열역학 모델이 없습니다. 축계 구동 요소(전동기·기어박스·터빈)로 수력 회로 요소가 아닙니다. 축 전파는 기존 경험 신호입니다.
- `RFCC_P-1301A`: 슬러리 펌프어라운드: FR 순환 루프이고 유량(379 m3/h)이 원료보다 커 단일 헤더 수지로 표현할 수 없습니다.
- `RFCC_P-1302A`: LCO 펌프어라운드: FR 순환 루프이고 유량(240 m3/h)이 원료보다 커 단일 헤더 수지로 표현할 수 없습니다.
- `RFCC_P-1402A`: 흡수유 펌프: GR→P-1402A→GR 루프와 가스 회수부 수력을 계산하지 않습니다.
- `RFCC_P-1403A`: 가스 회수 환류 펌프: GR→P-1403A→GR 루프와 가스 회수부 수력을 계산하지 않습니다.
- `RFCC_P-1501A`: LPG 이송 펌프: 원료 회로와 습가스 압축기(기상)로만 이어져 연결 회로로 풀 수 없고, 독립 회로 경계·검증을 수행하지 않았습니다.
- `RFCC_P-1502A`: 가솔린 이송 펌프: 원료 회로와 습가스 압축기(기상)로만 이어져 연결 회로로 풀 수 없고, 독립 회로 경계·검증을 수행하지 않았습니다.
- `RFCC_T-1401`: 증기 터빈 구동기: 축계 구동 요소(전동기·기어박스·터빈)로 수력 회로 요소가 아닙니다. 축 전파는 기존 경험 신호입니다.

## RHDS (RHDS)

| 설비 키 | 태그 키 | 물리량 | 단위 (YAML·SQL·CSV·3D) | SI 변환 | 주기 s (YAML·CSV) | 회로·역할 |
| --- | --- | --- | --- | --- | --- | --- |
| `RHDS_P-3101` | `RHDS_P-3101/FI-P3101` | 체적 유량 | m3/h · m3/h · m3/h · m3/h | m³/s = v/3,600 | 1 · 1 | `coupled:P-3101` source |
| `RHDS_P-3101` | `RHDS_P-3101/II-P3101` | 모터 전류(축동력 비례 대용) | A · A · A · A | 축동력 비 × 정상 전류 | 1 · 1 | `coupled:P-3101` source |
| `RHDS_P-3101` | `RHDS_P-3101/PI-D-P3101` | 토출 정압(게이지) | bar(g) · bar(g) · bar(g) · bar(g) | Pa(abs) = v·100,000 + 101,325 | 1 · 1 | `coupled:P-3101` source |
| `RHDS_P-3101` | `RHDS_P-3101/PI-S-P3101` | 흡입 정압(게이지) | bar(g) · bar(g) · bar(g) · bar(g) | Pa(abs) = v·100,000 + 101,325 | 1 · 1 | `coupled:P-3101` source |
| `RHDS_P-3101-S2` | `RHDS_P-3101-S2/FI-P3101S2` | 체적 유량 | m3/h · m3/h · m3/h · m3/h | m³/s = v/3,600 | 1 · 1 | `coupled:P-3101` source |
| `RHDS_P-3101-S2` | `RHDS_P-3101-S2/II-P3101S2` | 모터 전류(축동력 비례 대용) | A · A · A · A | 축동력 비 × 정상 전류 | 1 · 1 | `coupled:P-3101` source |
| `RHDS_P-3101-S2` | `RHDS_P-3101-S2/PI-D-P3101S2` | 토출 정압(게이지) | bar(g) · bar(g) · bar(g) · bar(g) | Pa(abs) = v·100,000 + 101,325 | 1 · 1 | `coupled:P-3101` source |
| `RHDS_P-3101-S2` | `RHDS_P-3101-S2/PI-S-P3101S2` | 흡입 정압(게이지) | bar(g) · bar(g) · bar(g) · bar(g) | Pa(abs) = v·100,000 + 101,325 | 1 · 1 | `coupled:P-3101` source |
| `RHDS_P-3102` | `RHDS_P-3102/FI-P3102` | 체적 유량 | m3/h · m3/h · m3/h · m3/h | m³/s = v/3,600 | 1 · 1 | `coupled:P-3101` downstream |
| `RHDS_P-3102` | `RHDS_P-3102/II-P3102` | 모터 전류(축동력 비례 대용) | A · A · A · A | 축동력 비 × 정상 전류 | 1 · 1 | `coupled:P-3101` downstream |
| `RHDS_P-3102` | `RHDS_P-3102/PI-D-P3102` | 토출 정압(게이지) | bar(g) · bar(g) · bar(g) · bar(g) | Pa(abs) = v·100,000 + 101,325 | 1 · 1 | `coupled:P-3101` downstream |
| `RHDS_P-3102` | `RHDS_P-3102/PI-S-P3102` | 흡입 정압(게이지) | bar(g) · bar(g) · bar(g) · bar(g) | Pa(abs) = v·100,000 + 101,325 | 1 · 1 | `coupled:P-3101` downstream |

공정 관계 `coupled:P-3101`: P-3101→P-3101-S2 (`RHDS/flow/16`) → P-3101-S2→MX (`RHDS/flow/17`) → MX→HX (`VRDS/flow/2`) → HX→HT (`VRDS/flow/3`) → HT→RX (`VRDS/flow/4`) → RX→HX (`VRDS/flow/5`) → HX→SP (`VRDS/flow/6`) → SP→FR (`VRDS/flow/7`) → FR→P-3102 (`RHDS/flow/12`)
가상 요소(온톨로지에 없는 계산 전용 키): virtual:discharge-header, virtual:other-branch, virtual:other-equivalent, virtual:process-equivalent, virtual:product-branch, virtual:product-control-valve, virtual:suction-boundary

제외 설비와 사유:

- `RHDS_C-3101`: 순환가스 기상 압축기: 압축성·다단 열역학·서지 모델이 없어 단상 비압축 회로로 표현할 수 없습니다. 기존 경험 신호 모드만 있습니다.
- `RHDS_C-3102`: 보충 수소 기상 압축기: 압축성·다단 열역학·서지 모델이 없어 단상 비압축 회로로 표현할 수 없습니다. 기존 경험 신호 모드만 있습니다.
- `RHDS_F-3101`: 팬 물리 후보(유량·차압·전류·회전수 태그 있음): CDU F-8201 과 같은 팬 회로 형식으로 만들 수 있으나 이 플랜트의 연소 공기 경로 경계와 Python·Modelica 검증을 수행하지 않았습니다. 미검증이므로 주입하지 않습니다.
- `RHDS_GB-3101`: 축계 구동 요소(전동기·기어박스·터빈)로 수력 회로 요소가 아닙니다. 축 전파는 기존 경험 신호입니다.
- `RHDS_M-3101`: 축계 구동 요소(전동기·기어박스·터빈)로 수력 회로 요소가 아닙니다. 축 전파는 기존 경험 신호입니다.
- `RHDS_M-3102`: 축계 구동 요소(전동기·기어박스·터빈)로 수력 회로 요소가 아닙니다. 축 전파는 기존 경험 신호입니다.
- `RHDS_P-3103`: 분별탑 환류 펌프: FR→P-3103→FR 루프와 탑 내부 수력을 계산하지 않으며 전류 태그가 없습니다.

## VRDS (VRDS)

| 설비 키 | 태그 키 | 물리량 | 단위 (YAML·SQL·CSV·3D) | SI 변환 | 주기 s (YAML·CSV) | 회로·역할 |
| --- | --- | --- | --- | --- | --- | --- |
| `VRDS_P-4101` | `VRDS_P-4101/FI-P4101` | 체적 유량 | m3/h · m3/h · m3/h · m3/h | m³/s = v/3,600 | 1 · 1 | `coupled:P-4101` source |
| `VRDS_P-4101` | `VRDS_P-4101/II-P4101` | 모터 전류(축동력 비례 대용) | A · A · A · A | 축동력 비 × 정상 전류 | 1 · 1 | `coupled:P-4101` source |
| `VRDS_P-4101` | `VRDS_P-4101/PI-D-P4101` | 토출 정압(게이지) | bar(g) · bar(g) · bar(g) · bar(g) | Pa(abs) = v·100,000 + 101,325 | 1 · 1 | `coupled:P-4101` source |
| `VRDS_P-4101` | `VRDS_P-4101/PI-S-P4101` | 흡입 정압(게이지) | bar(g) · bar(g) · bar(g) · bar(g) | Pa(abs) = v·100,000 + 101,325 | 1 · 1 | `coupled:P-4101` source |
| `VRDS_P-4101-S2` | `VRDS_P-4101-S2/FI-P4101S2` | 체적 유량 | m3/h · m3/h · m3/h · m3/h | m³/s = v/3,600 | 1 · 1 | `coupled:P-4101` source |
| `VRDS_P-4101-S2` | `VRDS_P-4101-S2/II-P4101S2` | 모터 전류(축동력 비례 대용) | A · A · A · A | 축동력 비 × 정상 전류 | 1 · 1 | `coupled:P-4101` source |
| `VRDS_P-4101-S2` | `VRDS_P-4101-S2/PI-D-P4101S2` | 토출 정압(게이지) | bar(g) · bar(g) · bar(g) · bar(g) | Pa(abs) = v·100,000 + 101,325 | 1 · 1 | `coupled:P-4101` source |
| `VRDS_P-4101-S2` | `VRDS_P-4101-S2/PI-S-P4101S2` | 흡입 정압(게이지) | bar(g) · bar(g) · bar(g) · bar(g) | Pa(abs) = v·100,000 + 101,325 | 1 · 1 | `coupled:P-4101` source |
| `VRDS_P-4102` | `VRDS_P-4102/FI-P4102` | 체적 유량 | m3/h · m3/h · m3/h · m3/h | m³/s = v/3,600 | 1 · 1 | `coupled:P-4101` downstream |
| `VRDS_P-4102` | `VRDS_P-4102/II-P4102` | 모터 전류(축동력 비례 대용) | A · A · A · A | 축동력 비 × 정상 전류 | 1 · 1 | `coupled:P-4101` downstream |
| `VRDS_P-4102` | `VRDS_P-4102/PI-D-P4102` | 토출 정압(게이지) | bar(g) · bar(g) · bar(g) · bar(g) | Pa(abs) = v·100,000 + 101,325 | 1 · 1 | `coupled:P-4101` downstream |
| `VRDS_P-4102` | `VRDS_P-4102/PI-S-P4102` | 흡입 정압(게이지) | bar(g) · bar(g) · bar(g) · bar(g) | Pa(abs) = v·100,000 + 101,325 | 1 · 1 | `coupled:P-4101` downstream |

공정 관계 `coupled:P-4101`: P-4101→P-4101-S2 (`VRDS/flow/16`) → P-4101-S2→MX (`VRDS/flow/17`) → MX→HX (`VRDS/flow/2`) → HX→HT (`VRDS/flow/3`) → HT→RX (`VRDS/flow/4`) → RX→HX (`VRDS/flow/5`) → HX→SP (`VRDS/flow/6`) → SP→FR (`VRDS/flow/7`) → FR→P-4102 (`VRDS/flow/12`)
가상 요소(온톨로지에 없는 계산 전용 키): virtual:discharge-header, virtual:other-branch, virtual:other-equivalent, virtual:process-equivalent, virtual:product-branch, virtual:product-control-valve, virtual:suction-boundary

제외 설비와 사유:

- `VRDS_C-4101`: 순환가스 기상 압축기: 압축성·다단 열역학·서지 모델이 없어 단상 비압축 회로로 표현할 수 없습니다. 기존 경험 신호 모드만 있습니다.
- `VRDS_C-4102`: 보충 수소 기상 압축기: 압축성·다단 열역학·서지 모델이 없어 단상 비압축 회로로 표현할 수 없습니다. 기존 경험 신호 모드만 있습니다.
- `VRDS_F-4101`: 팬 물리 후보(유량·차압·전류·회전수 태그 있음): CDU F-8201 과 같은 팬 회로 형식으로 만들 수 있으나 이 플랜트의 연소 공기 경로 경계와 Python·Modelica 검증을 수행하지 않았습니다. 미검증이므로 주입하지 않습니다.
- `VRDS_GB-4101`: 축계 구동 요소(전동기·기어박스·터빈)로 수력 회로 요소가 아닙니다. 축 전파는 기존 경험 신호입니다.
- `VRDS_M-4101`: 축계 구동 요소(전동기·기어박스·터빈)로 수력 회로 요소가 아닙니다. 축 전파는 기존 경험 신호입니다.
- `VRDS_M-4102`: 축계 구동 요소(전동기·기어박스·터빈)로 수력 회로 요소가 아닙니다. 축 전파는 기존 경험 신호입니다.
- `VRDS_P-4103`: 분별탑 환류 펌프: FR→P-4103→FR 루프와 탑 내부 수력을 계산하지 않으며 전류 태그가 없습니다.

## 대조 결과

불일치 없음.
