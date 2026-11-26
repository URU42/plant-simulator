# 플랜트 디지털 트윈 지원 매트릭스

> 2026-11-25 PU-D5a: 원료 펌프가 두 대(CDU·RFCC 병렬, HOU·RHDS·VRDS 직렬)로 늘었다. 이 문서의 단일 펌프 회로 수치·검증은 그 전 기록이다. 현재 회로는 `config/physics_*_integrated.yaml` 의 `station` 과 README 의 '두 번째 원료 펌프' 절을 본다.

작성 기준일 2026-11-24(오후 D0 재게이트·D1·D2 반영, 밤 D4 재검증 반영). 트윈 카탈로그(`collector/twin_catalog.py`)와 같은 날 실행 기록으로 생성. AI 편집 정리본이며 팀 승인 문서가 아니다. 물리 계수는 synthetic assumption 이다.

- **트윈 주입**: 트윈 패널·`/simulation/control` 에서 허용하는 조합(서버 강제, 그 외 422).
- **모델**: physical = 물리 회로 해가 압력·유량·전류·차압 태그를 정함, empirical = 기존 경험 신호(이 트윈에서 주입하지 않음).
- **검증**: CDU 는 D0 브라우저 E2E·비공개 DB 감사. HOU·RFCC·RHDS·VRDS 는 D1·D2-A(생성기·고장 엔진 결합 시험)·D2-B(OpenModelica, NPSH 포함)를 통과했고, 강화된 D0 기준으로 다시 실행한 D3·D4 재검증에서 다섯 플랜트 브라우저 E2E·비공개 DB 감사를 통과했다([plant-twin-d4.md](plant-twin-d4.md) "재검증" 절). CDU 지원 시나리오도 같은 D4 재검증에서 다시 통과했다. 오전 D4 기록(이전 D0 기준)은 판정에 쓰지 않는다.

## CDU (PL-CDU)

회로: `coupled:P-8101A` P-8101A → DS → HX → HT → AT → P-8401A; `single:P-8303A` AT → P-8303A → virtual:residue-boundary; `single:F-8201` virtual:ambient-air → F-8201 → HT

| 대상 ID | 역할·회로 | 시나리오 | 트윈 주입 | 모델 | 후속(연결) 대상 | 검증 결과 | 제외 사유 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| P-8101A | source `coupled:P-8101A` | PM-SUCTION-LOSS | 예 | physical | P-8401A | D0 E2E·감사 통과; 경보 PI-8101A; 연결 영향 0 |  |
| P-8101A | source `coupled:P-8101A` | OP-THROUGHPUT-CHANGE | 예 | physical | P-8401A | D0 E2E·감사 통과; 한계 미교차; 연결 영향 0 |  |
| P-8101A | source `coupled:P-8101A` | PM-IMPELLER-WEAR | 예 | physical | P-8401A | D0 E2E·감사 통과; 한계 미교차; 연결 영향 1; 손상 유지→수리 |  |
| P-8101A | source `coupled:P-8101A` | PM-DISCHARGE-VALVE-RESTRICTION | 예 | physical | P-8401A | D0 E2E·감사 통과; 한계 미교차; 연결 영향 2 |  |
| P-8102A | 회로 밖 | PM-SUCTION-LOSS | 아니오 | empirical | — | 주입 차단(UI·서버) | 탈염 세정수 펌프: 흡입압 태그가 없고 탈염기(DS) 혼합·수위 경계를 계산하지 않아 독립 경계조건을 검증할 수 없습니다. |
| F-8201 | single `single:F-8201` | FN-IMPELLER-FOULING | 예 | physical |  | D0 E2E·감사 통과; 한계 미교차; 연결 영향 0; 손상 유지→수리 |  |
| F-8201 | single `single:F-8201` | FN-INLET-RESTRICTION | 예 | physical |  | D0 E2E·감사 통과; 경보 FI-8201A; 연결 영향 0 |  |
| P-8301A | 회로 밖 | PM-SUCTION-LOSS | 아니오 | empirical | — | 주입 차단(UI·서버) | 중간 순환환류 펌프: AT→HX→AT 순환 루프와 탑 내부 수력을 계산하지 않으며 전류 태그가 없습니다. |
| P-8302A | 회로 밖 | PM-SUCTION-LOSS | 아니오 | empirical | — | 주입 차단(UI·서버) | 탑정 환류 펌프: DR→AT 환류 루프의 드럼 수위·탑 압력 경계를 계산하지 않으며 전류 태그가 없습니다. |
| P-8303A | single `single:P-8303A` | PM-SUCTION-LOSS | 예 | physical |  | D0 E2E·감사 통과; 한계 미교차; 연결 영향 0 |  |
| P-8303A | single `single:P-8303A` | PM-SUCTION-HOT | 예 | physical |  | D0 E2E·감사 통과; 경보 PI-8303B, FI-8303A; 연결 영향 0 |  |
| P-8303A | single `single:P-8303A` | PM-IMPELLER-WEAR | 예 | physical |  | D0 E2E·감사 통과; 한계 미교차; 연결 영향 0; 손상 유지→수리 |  |
| P-8401A | downstream `coupled:P-8101A` | PM-SUCTION-LOSS | 예 | physical | P-8101A | D0 E2E·감사 통과; 경보 FI-8401A; 연결 영향 3 |  |
| P-8401A | downstream `coupled:P-8101A` | PM-IMPELLER-WEAR | 예 | physical | P-8101A | D0 E2E·감사 통과; 한계 미교차; 연결 영향 0; 손상 유지→수리 |  |

## HOU (PL-HOU)

회로: `coupled:P-2101` P-2101 → MIX → HX → FURN → REACT → HX → SEP → STRIP → FRAC → P-2701

| 대상 ID | 역할·회로 | 시나리오 | 트윈 주입 | 모델 | 후속(연결) 대상 | 검증 결과 | 제외 사유 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| P-2101 | source `coupled:P-2101` | PM-SUCTION-LOSS | 예 | physical | P-2701 | D2-A 생성기 시험·D2-B Modelica·D4 브라우저 E2E·DB 감사 통과 |  |
| P-2101 | source `coupled:P-2101` | OP-THROUGHPUT-CHANGE | 예 | physical | P-2701 | D2-A 생성기 시험·D2-B Modelica·D4 브라우저 E2E·DB 감사 통과 |  |
| P-2101 | source `coupled:P-2101` | PM-IMPELLER-WEAR | 예 | physical | P-2701 | D2-A 생성기 시험·D2-B Modelica·D4 브라우저 E2E·DB 감사 통과 |  |
| C-2201 | 회로 밖 | OP-LOAD-CHANGE | 아니오 | empirical | — | 주입 차단(UI·서버) | 보충 수소 기상 압축기: 압축성·다단 열역학·서지 모델이 없어 단상 비압축 회로로 표현할 수 없습니다. 기존 경험 신호 모드만 있습니다. |
| C-2201 | 회로 밖 | RC-VALVE-LEAK | 아니오 | empirical | — | 주입 차단(UI·서버) | 보충 수소 기상 압축기: 압축성·다단 열역학·서지 모델이 없어 단상 비압축 회로로 표현할 수 없습니다. 기존 경험 신호 모드만 있습니다. |
| C-2201 | 회로 밖 | RC-LIQUID-INGRESS | 아니오 | empirical | — | 주입 차단(UI·서버) | 보충 수소 기상 압축기: 압축성·다단 열역학·서지 모델이 없어 단상 비압축 회로로 표현할 수 없습니다. 기존 경험 신호 모드만 있습니다. |
| C-2301 | 회로 밖 | CC-SURGE | 아니오 | empirical | — | 주입 차단(UI·서버) | 순환가스 기상 압축기: 압축성·다단 열역학·서지 모델이 없어 단상 비압축 회로로 표현할 수 없습니다. 기존 경험 신호 모드만 있습니다. |
| C-2301 | 회로 밖 | OP-LOAD-CHANGE | 아니오 | empirical | — | 주입 차단(UI·서버) | 순환가스 기상 압축기: 압축성·다단 열역학·서지 모델이 없어 단상 비압축 회로로 표현할 수 없습니다. 기존 경험 신호 모드만 있습니다. |
| FN-2401 | 회로 밖 | FN-IMPELLER-FOULING | 아니오 | empirical | — | 주입 차단(UI·서버) | 팬 물리 후보(유량·차압·전류·회전수 태그 있음): CDU F-8201 과 같은 팬 회로 형식으로 만들 수 있으나 이 플랜트의 연소 공기 경로 경계와 Python·Modelica 검증을 수행하지 않았습니다. 미검증이므로 주입하지 않습니다. |
| P-2501 | 회로 밖 | PM-SUCTION-LOSS | 아니오 | empirical | — | 주입 차단(UI·서버) | 압축기 윤활유 펌프: 흡입압 태그가 없고 윤활유 저장조·베어링 분배 회로 경계를 계산하지 않습니다. |
| P-2601 | 회로 밖 | PM-SUCTION-LOSS | 아니오 | empirical | — | 주입 차단(UI·서버) | 분별탑 환류 펌프: FRAC→P-2601→FRAC 환류 루프와 탑 내부 수력을 계산하지 않습니다. |
| P-2701 | downstream `coupled:P-2101` | PM-SUCTION-LOSS | 예 | physical | P-2101 | D2-A 생성기 시험·D2-B Modelica·D4 브라우저 E2E·DB 감사 통과 |  |
| P-2701 | downstream `coupled:P-2101` | PM-IMPELLER-WEAR | 예 | physical | P-2101 | D2-A 생성기 시험·D2-B Modelica·D4 브라우저 E2E·DB 감사 통과 |  |

## RFCC (PL-RFCC)

회로: `coupled:P-1101A` P-1101A → RX → FR → P-1303A

| 대상 ID | 역할·회로 | 시나리오 | 트윈 주입 | 모델 | 후속(연결) 대상 | 검증 결과 | 제외 사유 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| P-1101A | source `coupled:P-1101A` | PM-SUCTION-LOSS | 예 | physical | P-1303A | D2-A 생성기 시험·D2-B Modelica·D4 브라우저 E2E·DB 감사 통과 |  |
| P-1101A | source `coupled:P-1101A` | OP-THROUGHPUT-CHANGE | 예 | physical | P-1303A | D2-A 생성기 시험·D2-B Modelica·D4 브라우저 E2E·DB 감사 통과 |  |
| P-1101A | source `coupled:P-1101A` | PM-IMPELLER-WEAR | 예 | physical | P-1303A | D2-A 생성기 시험·D2-B Modelica·D4 브라우저 E2E·DB 감사 통과 |  |
| B-1201 | 회로 밖 | CC-SURGE | 아니오 | empirical | — | 주입 차단(UI·서버) | 주 공기 블로어(E-1202 축 구동): 압축성 공기·팽창기 축 결합 열역학 모델이 없습니다. |
| E-1202 | 회로 밖 | EX-CATALYST-EROSION | 아니오 | empirical | — | 주입 차단(UI·서버) | 동력 회수 팽창기: 배가스 팽창 열역학 모델이 없습니다. 축계 구동 요소(전동기·기어박스·터빈)로 수력 회로 요소가 아닙니다. 축 전파는 기존 경험 신호입니다. |
| P-1301A | 회로 밖 | PM-SUCTION-LOSS | 아니오 | empirical | — | 주입 차단(UI·서버) | 슬러리 펌프어라운드: FR 순환 루프이고 유량(379 m3/h)이 원료보다 커 단일 헤더 수지로 표현할 수 없습니다. |
| P-1302A | 회로 밖 | PM-SUCTION-LOSS | 아니오 | empirical | — | 주입 차단(UI·서버) | LCO 펌프어라운드: FR 순환 루프이고 유량(240 m3/h)이 원료보다 커 단일 헤더 수지로 표현할 수 없습니다. |
| P-1303A | downstream `coupled:P-1101A` | PM-SUCTION-LOSS | 예 | physical | P-1101A | D2-A 생성기 시험·D2-B Modelica·D4 브라우저 E2E·DB 감사 통과 |  |
| P-1303A | downstream `coupled:P-1101A` | PM-IMPELLER-WEAR | 예 | physical | P-1101A | D2-A 생성기 시험·D2-B Modelica·D4 브라우저 E2E·DB 감사 통과 |  |
| C-1401 | 회로 밖 | CC-SURGE | 아니오 | empirical | — | 주입 차단(UI·서버) | 습가스 기상 압축기: 압축성·다단 열역학·서지 모델이 없어 단상 비압축 회로로 표현할 수 없습니다. 기존 경험 신호 모드만 있습니다. |
| T-1401 | 회로 밖 | ST-WATER-INDUCTION | 아니오 | empirical | — | 주입 차단(UI·서버) | 증기 터빈 구동기: 축계 구동 요소(전동기·기어박스·터빈)로 수력 회로 요소가 아닙니다. 축 전파는 기존 경험 신호입니다. |
| T-1401 | 회로 밖 | OP-LOAD-CHANGE | 아니오 | empirical | — | 주입 차단(UI·서버) | 증기 터빈 구동기: 축계 구동 요소(전동기·기어박스·터빈)로 수력 회로 요소가 아닙니다. 축 전파는 기존 경험 신호입니다. |
| P-1402A | 회로 밖 | PM-SUCTION-LOSS | 아니오 | empirical | — | 주입 차단(UI·서버) | 흡수유 펌프: GR→P-1402A→GR 루프와 가스 회수부 수력을 계산하지 않습니다. |
| P-1403A | 회로 밖 | PM-SUCTION-LOSS | 아니오 | empirical | — | 주입 차단(UI·서버) | 가스 회수 환류 펌프: GR→P-1403A→GR 루프와 가스 회수부 수력을 계산하지 않습니다. |
| P-1501A | 회로 밖 | PM-SUCTION-LOSS | 아니오 | empirical | — | 주입 차단(UI·서버) | LPG 이송 펌프: 원료 회로와 습가스 압축기(기상)로만 이어져 연결 회로로 풀 수 없고, 독립 회로 경계·검증을 수행하지 않았습니다. |
| P-1502A | 회로 밖 | PM-SUCTION-LOSS | 아니오 | empirical | — | 주입 차단(UI·서버) | 가솔린 이송 펌프: 원료 회로와 습가스 압축기(기상)로만 이어져 연결 회로로 풀 수 없고, 독립 회로 경계·검증을 수행하지 않았습니다. |

## RHDS (PL-RHDS)

회로: `coupled:P-3101` P-3101 → MX → HX → HT → RX → HX → SP → FR → P-3102

| 대상 ID | 역할·회로 | 시나리오 | 트윈 주입 | 모델 | 후속(연결) 대상 | 검증 결과 | 제외 사유 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| P-3101 | source `coupled:P-3101` | PM-SUCTION-LOSS | 예 | physical | P-3102 | D2-A 생성기 시험·D2-B Modelica·D4 브라우저 E2E·DB 감사 통과 |  |
| P-3101 | source `coupled:P-3101` | OP-THROUGHPUT-CHANGE | 예 | physical | P-3102 | D2-A 생성기 시험·D2-B Modelica·D4 브라우저 E2E·DB 감사 통과 |  |
| P-3101 | source `coupled:P-3101` | PM-IMPELLER-WEAR | 예 | physical | P-3102 | D2-A 생성기 시험·D2-B Modelica·D4 브라우저 E2E·DB 감사 통과 |  |
| M-3102 | 회로 밖 | OP-LOAD-CHANGE | 아니오 | empirical | — | 주입 차단(UI·서버) | 축계 구동 요소(전동기·기어박스·터빈)로 수력 회로 요소가 아닙니다. 축 전파는 기존 경험 신호입니다. |
| C-3102 | 회로 밖 | RC-VALVE-LEAK | 아니오 | empirical | — | 주입 차단(UI·서버) | 보충 수소 기상 압축기: 압축성·다단 열역학·서지 모델이 없어 단상 비압축 회로로 표현할 수 없습니다. 기존 경험 신호 모드만 있습니다. |
| C-3102 | 회로 밖 | RC-LIQUID-INGRESS | 아니오 | empirical | — | 주입 차단(UI·서버) | 보충 수소 기상 압축기: 압축성·다단 열역학·서지 모델이 없어 단상 비압축 회로로 표현할 수 없습니다. 기존 경험 신호 모드만 있습니다. |
| M-3101 | 회로 밖 | OP-LOAD-CHANGE | 아니오 | empirical | — | 주입 차단(UI·서버) | 축계 구동 요소(전동기·기어박스·터빈)로 수력 회로 요소가 아닙니다. 축 전파는 기존 경험 신호입니다. |
| GB-3101 | 회로 밖 | GB-LUBE-WEAR | 아니오 | empirical | — | 주입 차단(UI·서버) | 축계 구동 요소(전동기·기어박스·터빈)로 수력 회로 요소가 아닙니다. 축 전파는 기존 경험 신호입니다. |
| C-3101 | 회로 밖 | CC-SURGE | 아니오 | empirical | — | 주입 차단(UI·서버) | 순환가스 기상 압축기: 압축성·다단 열역학·서지 모델이 없어 단상 비압축 회로로 표현할 수 없습니다. 기존 경험 신호 모드만 있습니다. |
| F-3101 | 회로 밖 | FN-IMPELLER-FOULING | 아니오 | empirical | — | 주입 차단(UI·서버) | 팬 물리 후보(유량·차압·전류·회전수 태그 있음): CDU F-8201 과 같은 팬 회로 형식으로 만들 수 있으나 이 플랜트의 연소 공기 경로 경계와 Python·Modelica 검증을 수행하지 않았습니다. 미검증이므로 주입하지 않습니다. |
| P-3102 | downstream `coupled:P-3101` | PM-SUCTION-LOSS | 예 | physical | P-3101 | D2-A 생성기 시험·D2-B Modelica·D4 브라우저 E2E·DB 감사 통과 |  |
| P-3102 | downstream `coupled:P-3101` | PM-SUCTION-HOT | 아니오 | empirical | — | 주입 차단(UI·서버) | 흡입 유체 온도 상승의 증기압·NPSH 영향을 이 결합 회로가 계산하지 않아(캐비테이션 모델 없음) 일부만 물리로 표현됩니다. 트윈에서 주입하지 않습니다. |
| P-3102 | downstream `coupled:P-3101` | PM-IMPELLER-WEAR | 예 | physical | P-3101 | D2-A 생성기 시험·D2-B Modelica·D4 브라우저 E2E·DB 감사 통과 |  |
| P-3103 | 회로 밖 | PM-SUCTION-LOSS | 아니오 | empirical | — | 주입 차단(UI·서버) | 분별탑 환류 펌프: FR→P-3103→FR 루프와 탑 내부 수력을 계산하지 않으며 전류 태그가 없습니다. |

## VRDS (PL-VRDS)

회로: `coupled:P-4101` P-4101 → MX → HX → HT → RX → HX → SP → FR → P-4102

| 대상 ID | 역할·회로 | 시나리오 | 트윈 주입 | 모델 | 후속(연결) 대상 | 검증 결과 | 제외 사유 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| P-4101 | source `coupled:P-4101` | PM-SUCTION-LOSS | 예 | physical | P-4102 | D2-A 생성기 시험·D2-B Modelica·D4 브라우저 E2E·DB 감사 통과 |  |
| P-4101 | source `coupled:P-4101` | OP-THROUGHPUT-CHANGE | 예 | physical | P-4102 | D2-A 생성기 시험·D2-B Modelica·D4 브라우저 E2E·DB 감사 통과 |  |
| P-4101 | source `coupled:P-4101` | PM-IMPELLER-WEAR | 예 | physical | P-4102 | D2-A 생성기 시험·D2-B Modelica·D4 브라우저 E2E·DB 감사 통과 |  |
| M-4102 | 회로 밖 | OP-LOAD-CHANGE | 아니오 | empirical | — | 주입 차단(UI·서버) | 축계 구동 요소(전동기·기어박스·터빈)로 수력 회로 요소가 아닙니다. 축 전파는 기존 경험 신호입니다. |
| C-4102 | 회로 밖 | RC-VALVE-LEAK | 아니오 | empirical | — | 주입 차단(UI·서버) | 보충 수소 기상 압축기: 압축성·다단 열역학·서지 모델이 없어 단상 비압축 회로로 표현할 수 없습니다. 기존 경험 신호 모드만 있습니다. |
| C-4102 | 회로 밖 | RC-LIQUID-INGRESS | 아니오 | empirical | — | 주입 차단(UI·서버) | 보충 수소 기상 압축기: 압축성·다단 열역학·서지 모델이 없어 단상 비압축 회로로 표현할 수 없습니다. 기존 경험 신호 모드만 있습니다. |
| C-4101 | 회로 밖 | CC-SURGE | 아니오 | empirical | — | 주입 차단(UI·서버) | 순환가스 기상 압축기: 압축성·다단 열역학·서지 모델이 없어 단상 비압축 회로로 표현할 수 없습니다. 기존 경험 신호 모드만 있습니다. |
| F-4101 | 회로 밖 | FN-IMPELLER-FOULING | 아니오 | empirical | — | 주입 차단(UI·서버) | 팬 물리 후보(유량·차압·전류·회전수 태그 있음): CDU F-8201 과 같은 팬 회로 형식으로 만들 수 있으나 이 플랜트의 연소 공기 경로 경계와 Python·Modelica 검증을 수행하지 않았습니다. 미검증이므로 주입하지 않습니다. |
| P-4102 | downstream `coupled:P-4101` | PM-SUCTION-LOSS | 예 | physical | P-4101 | D2-A 생성기 시험·D2-B Modelica·D4 브라우저 E2E·DB 감사 통과 |  |
| P-4102 | downstream `coupled:P-4101` | PM-SUCTION-HOT | 아니오 | empirical | — | 주입 차단(UI·서버) | 흡입 유체 온도 상승의 증기압·NPSH 영향을 이 결합 회로가 계산하지 않아(캐비테이션 모델 없음) 일부만 물리로 표현됩니다. 트윈에서 주입하지 않습니다. |
| P-4102 | downstream `coupled:P-4101` | PM-IMPELLER-WEAR | 예 | physical | P-4101 | D2-A 생성기 시험·D2-B Modelica·D4 브라우저 E2E·DB 감사 통과 |  |
| P-4103 | 회로 밖 | PM-SUCTION-LOSS | 아니오 | empirical | — | 주입 차단(UI·서버) | 분별탑 환류 펌프: FR→P-4103→FR 루프와 탑 내부 수력을 계산하지 않으며 전류 태그가 없습니다. |

