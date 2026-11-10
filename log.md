# log

## 2026-11-10 · 구성안과 엑셀 샘플 데이터 대조

대상: `config/plant_{cdu,hou,rhds,vrds}.yaml` ↔ 샘플 묶음 Excel(`cdu-sample/data/*.xlsx`, `hou-plant-sample/data/*.xlsx`,
`RHDS_VRDS_v3/data/*.xlsx`)의 `설비 설명`·`설비 태그` 시트. 추출본(openpyxl)과 구성안을 항목별로 기계 대조했다.

### 마스터 값 — 불일치 없음

| 항목 | 대조 수 | 결과 |
| --- | --- | --- |
| 태그 (설비 ID, 태그명) | 193 | 모두 일치. 구성안에만 있는 태그 없음 |
| 한계값 칸 (경고/알람/정지 상·하한) | 380 | 모두 일치. 빈 칸은 구성안에도 없음 |
| 계층 (Plant · Unit Group · Unit ID 와 이름), 설비 이름 | 26대 | 모두 일치 |
| 태그 설명, 공학 단위, 기록 간격(온도 10초·나머지 1초) | 193 | 모두 일치 |
| 정상 기준 원문(`normal_ref`), 한계값 근거(`limit_basis`) | 193 | 모두 일치 (문자열 그대로) |

### 값이 만들어지는 방식 — 의도된 차이

설계(`docs/superpowers/specs/2026-11-09-multi-plant-design.md`)대로 재생이 아니라 생성이므로 아래는 엑셀과 다르다.

| 항목 | 엑셀 | 시뮬레이터 |
| --- | --- | --- |
| 측정값 자체 | 83시간 20분(2026-11-01 00:00 ~ 11-04 11:20 KST)의 고정 기록 | 끝없이 새로 생성. 같은 시각이라도 엑셀 값과 다르다 |
| 기준값 `normal.base` | HOU 는 "정상 기준" 단일값(예: TI-2401A 58), CDU 는 범위(예: 1.4~2.8), RHDS/VRDS 는 없음 | 데이터 첫 6시간 Good·Uncertain 평균. HOU 61개 중 18개가 명시값과 2% 넘게 차이(최대 ZI-2301 −7 %: 0.0558 vs 0.06, VI-2401B −5.4 %, TI-2401B −4.8 %). CDU 는 모두 범위 안 |
| 잡음·변동 | 묶음마다 다른 생성기(HOU 흰 잡음+느린 성분, CDU 스파이크, RHDS 지연 모델) | RFCC 신호 모델 하나. 잡음 크기(`noise_sd`)는 첫 1시간 추세 제거 표준편차 × 0.9. HOU 생성기 흰 잡음과 비교하면 대체로 ±20 % 안(VI-2101A 0.074 vs 0.08, PI-2101B 0.31 vs 0.25, FI-2101 0.94 vs 0.6) |
| 이상 이벤트 | 묶음별 고유 모양(CDU 지수형 열화 b^1.8, HOU 불규칙 버스트, RHDS 지연 회복) | 모두 `swell` 포락선(상승 25 % · 유지 · 회복 30 %)으로 근사, 매주 같은 요일·시각에 반복. 크기는 평가 정답표 수치를 따랐고, RHDS/VRDS 는 수치가 없어 설계에서 정함 |
| 운전 변경 | CDU 원유 탱크 전환(11-02 22:00, 3시간), HOU C-2201 부하 증가(40~54시간) | 넣지 않음 (설계 비목표) |
| 품질 표기 | `Scan Off`(공백 포함) | `ScanOff` (기획서 표기) |
| HOU 랜덤 품질 저하 | 생성기: 샘플당 Uncertain 3e-5(10초 태그 ×10), 태그당 0~2회 Bad 구간 30~400초 | `random_quality` 로 같은 비율을 결정적으로 재현. 자리와 길이는 다르다 |
| 소수 자릿수 | 값의 자릿수(CDU 4, HOU 0~3, RHDS/VRDS 0~3) | 데이터에서 읽어 그대로(`decimals`) |

### 엑셀에 없어서 정한 것

- 설비 유형 코드: Pump → PM, Compressor(왕복동 포함) → CC, Fan → FN.
- 신호 종류(`signal`): 측정 구분·단위·설명으로 대응(진동 um pk-pk → shaft_vibration, 온도 중 윤활/오일 → oil_temp, 유체/인출 → process_temp, 토출 → discharge_temp, 흡입 → suction_temp, 그 외 → bearing_temp; 압력 중 윤활 → oil_pressure, 흡입/입구 → suction_pressure, 그 외 → discharge_pressure; 차압 → differential_pressure).
- `seed_key`(공장 접두어 `cdu-e01`, `cdu-r001` …), VRDS 시드 20261111(RHDS 20261110 과 쌍둥이 방지).

### 범위 밖

- RFCC 구성안은 이 엑셀들이 아니라 같은 묶음의 `support/config.json` 에서 옮겼다. HDS 는 기획서 기준이다.
- 엑셀의 측정 시트(원시 시계열)와 생성값의 수치 비교는 하지 않았다. 생성이 목적이라 같을 수 없다.

### 남겨 둔 선택지

HOU 기준값을 데이터 평균 대신 표의 "정상 기준" 단일값으로 바꾸려면 생성 스크립트 한 줄과 yaml 재생성이면 된다.
