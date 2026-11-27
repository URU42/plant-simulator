# 플랜트 대표 회로 D2-A·D2-B (생성기 연동 · OpenModelica 교차 검증)

> 2026-11-25 PU-D5a: 원료 펌프가 두 대(CDU·RFCC 병렬, HOU·RHDS·VRDS 직렬)로 늘었다. 이 문서의 단일 펌프 회로 수치·검증은 그 전 기록이다. 현재 회로는 `config/physics_*_integrated.yaml` 의 `station` 과 README 의 '두 번째 원료 펌프' 절을 본다.

작성 기준일 2026-11-24. AI 편집 정리본이며 팀 승인 문서가 아니다. 물리 계수·NPSH·곡선은 synthetic assumption 이다. 기준선은 [CDU D0](cdu-d0-twin.md), 회로·태그 매핑은 [D1 대조](twin-ontology-mapping.md), 설비별 지원 범위는 [지원 매트릭스](twin-support-matrix.md).

## D2-A 명시 시나리오 매핑 (`config/physics_<plant>_twin.yaml` 의 `scenarios`)

| 플랜트 | 설비(역할) | 트윈 물리 시나리오(기존 카탈로그 ID) | 물리 파라미터 | 매핑에서 뺀 조합·사유 |
| --- | --- | --- | --- | --- |
| CDU | P-8101A(입력) | PM-SUCTION-LOSS, PM-IMPELLER-WEAR, PM-DISCHARGE-VALVE-RESTRICTION, OP-THROUGHPUT-CHANGE | 흡입 저항, Q–H·효율, 제품 분기 밸브 개도 | — |
| CDU | P-8401A(후속) | PM-SUCTION-LOSS, PM-IMPELLER-WEAR | 후속 흡입 저항, Q–H·효율 | — |
| CDU | P-8303A(단일) | PM-SUCTION-LOSS, PM-SUCTION-HOT, PM-IMPELLER-WEAR | 흡입 저항, 흡입 유체 온도(증기압·NPSH), Q–H·효율 | — |
| CDU | F-8201(단일 팬) | FN-IMPELLER-FOULING, FN-INLET-RESTRICTION | 팬 압력 곡선·효율, 흡입 필터·댐퍼 저항 | — |
| HOU | P-2101 / P-2701 | 입력: PM-SUCTION-LOSS, PM-IMPELLER-WEAR, OP-THROUGHPUT-CHANGE · 후속: PM-SUCTION-LOSS, PM-IMPELLER-WEAR | 흡입 저항, Q–H·효율, (처리량) 가상 유량 조절 밸브 | 밸브 제한: CDU 전용(밸브 설비·태그 없음) |
| RFCC | P-1101A / P-1303A | 같음 | 같음 | 같음 |
| RHDS | P-3101 / P-3102 | 같음 | 같음 | P-3102 PM-SUCTION-HOT: 결합 회로에 흡입 온도 경계가 없어 일부만 물리 |
| VRDS | P-4101 / P-4102 | 같음 | 같음 | P-4102 PM-SUCTION-HOT: 같은 사유 |

- 매핑에 없는 모드는 물리 파라미터를 쓰지 않는 경험 신호로만 동작하고(물리 태그에는 z 효과 없음) 트윈에서 주입하지 않는다.
- 근거 구분: 카탈로그 시나리오마다 `basis` 로 **물리 계산 태그**(회로를 다시 풀어 후속 설비 포함), **경험 신호 태그**(기존 합성 효과), **온톨로지 연결**(회로 밖 간선: 예 HOU P-2701→MIX 재순환, RFCC P-1303A→FR 환류, 물리 전파 아님)을 따로 내리고 트윈 패널에 표시한다.
- 처리량 감소(OP-THROUGHPUT-CHANGE)는 기존 카탈로그가 다섯 원료 펌프에 정의한 정상 운전 변화다. 회로의 가상 유량 조절 밸브(`virtual:product-control-valve`, 온톨로지·센서 없음)로 계산한다.
- 팬(HOU FN-2401, RHDS F-3101, VRDS F-4101)·압축기·축계 설비는 물리 모델 미검증·미지원으로 매트릭스에 사유와 함께 남겼다. 펌프 식을 팬·압축기에 적용하지 않았다.

### D2-A 시험 (`tests/test_d2a_plants.py`, 34개)

- 네 플랜트 매핑의 설비·시나리오 20개 각각: 주입 → 단계 상승 → 최대 → 정지 → (손상형) 유지·수리 → 복귀, 물리 태그 = 회로 해 변화(z 중복 없음), 연결 펌프 변화, 다른 설비·태그 불변, 경험 신호는 주입 설비에만, 복귀 후 정상 계측과 동일.
- 다른 공장 생성기 무영향, physics 없는 기존 경로 유지(물리 전용 모드 미노출), 회로 설비가 없는 Unit 만 맡은 생성기는 물리 계산 없이 기존 값과 동일.
- 계측 고착·누락이 물리 상태와 분리(고착 구간 계측 고정, 같은 구간 물리 상태 변화, 설비 전체 누락 5초), 생성 시각 KST·API 측정 시각 UTC(같은 순간), 일반 1초·온도 10초.

## D2-B OpenModelica (NPSH·캐비테이션 포함)

결합 회로 다섯 개와 CDU 단일 회로 두 개를 각각 실제 실행해 모두 통과했다(결합 216/216 ×5, P-8303A 108/108, F-8201 48/48). 결합 회로의 입력·후속 펌프에 합성 증기압·NPSHr 를 넣었고, 네 플랜트는 막힘 크기를 "흡입압 목표"와 "NPSH 여유비 0.85" 중 먼저 닿는 쪽으로 다시 보정했다. 상세 표·허용오차·재사용 근거·미검증 범위: [models/modelica/REPORT.md](../models/modelica/REPORT.md) 마지막 절.

## 미실행·한계

- 이 문서 작성 시점에는 D3·D4 를 네 플랜트에 대해 재실행하지 않았다. 이후 D3 배치 재검사와 D4 재검증(다섯 플랜트 브라우저 E2E·비공개 DB 감사)을 통과했다([plant-twin-d4.md](plant-twin-d4.md) "재검증" 절).
- 작성 시점의 CDU 브라우저 E2E 는 결합 회로 NPSH 추가 전 실행이었다(해 변화 최대 0.04 %). NPSH 추가 후 D4 재검증에서 CDU 11개 시나리오를 다시 통과했다.
- 직렬 등가 회로 특성상 입력 쪽 고장에서도 후속 펌프 흡입 여유가 줄어 캐비테이션이 시작될 수 있다(HOU 입력 막힘 시 후속 여유비 0.99). 실제 설비의 중간 드럼·액위 제어는 계산하지 않는다.
