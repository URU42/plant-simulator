# 공장별 설비·태그

배포 중인 다섯 공장(RFCC·CDU·HOU·RHDS·VRDS)의 설비와 태그 목록이다. `table-plan.md` 3절을 따로 뺀 것이다.

- **기준**: 2026-11-11
- **합계**: 설비 38대 · 태그 274개. 태그는 `(equip_id, tag_id)` 로 식별한다

- 유형: PM 펌프, FN 팬, CC 압축기, BL 송풍기, EX 팽창 터빈, ST 증기 터빈.
- 한계값 칸은 `경고 / 알람 / 정지` 순서이고, `—` 는 자료에 값이 없는 칸이다 (DB 에는 NULL).

## 1. 가상 정유단지 RFCC — `rfcc` (`PL-RFCC`)

설비 12대 · 태그 81개

| 설비      | 이름                                    | 유형           | 공정 그룹 / 단위                   | 태그 수 |
| --------- | --------------------------------------- | -------------- | ---------------------------------- | ------: |
| `P-1101A` | Feed Charge Pump                        | PM 펌프        | 원료 공급 / 원료 공급              |       6 |
| `B-1201`  | Main Air Blower (구동: E-1202)          | BL 송풍기      | 반응·재생 / 촉매 재생              |       8 |
| `E-1202`  | Power Recovery Expander                 | EX 팽창 터빈   | 반응·재생 / 동력 회수              |       8 |
| `P-1301A` | Slurry Pumparound Pump                  | PM 펌프        | 주분별 / 슬러리·LCO 순환·탑정 처리 |       6 |
| `P-1302A` | LCO Pumparound Pump                     | PM 펌프        | 주분별 / 슬러리·LCO 순환·탑정 처리 |       6 |
| `P-1303A` | Overhead Reflux Pump                    | PM 펌프        | 주분별 / 슬러리·LCO 순환·탑정 처리 |       6 |
| `C-1401`  | Wet Gas Compressor (구동: T-1401)       | CC 원심 압축기 | 가스 압축·회수 / 습가스 압축       |      10 |
| `T-1401`  | Wet Gas Compressor Driver Steam Turbine | ST 증기 터빈   | 가스 압축·회수 / 습가스 압축 구동  |       7 |
| `P-1402A` | Absorber Oil Pump                       | PM 펌프        | 가스 압축·회수 / 흡수·분별·안정화  |       6 |
| `P-1403A` | Gas Recovery Reflux Pump                | PM 펌프        | 가스 압축·회수 / 흡수·분별·안정화  |       6 |
| `P-1501A` | LPG Transfer Pump                       | PM 펌프        | 제품 처리 / LPG·가솔린 처리        |       6 |
| `P-1502A` | Gasoline Transfer Pump                  | PM 펌프        | 제품 처리 / LPG·가솔린 처리        |       6 |

<details><summary>태그 목록 (한계값: 경고 / 알람 / 정지)</summary>

| 설비      | 태그         | 신호               | 단위     | 설명                                     | 주기(s) | 하한 | 상한 |
| --------- | ------------ | ------------------ | -------- | ---------------------------------------- | ------: | ---- | ---- |
| `P-1101A` | `VI-P-1101A` | vibration          | mm/s RMS | 펌프 구동측 케이싱 진동 속도 Overall RMS |       1 | —    | —    |
| `P-1101A` | `TI-P-1101A` | bearing_temp       | degC     | 펌프 구동측 베어링 온도                  |      10 | —    | —    |
| `P-1101A` | `PS-P-1101A` | suction_pressure   | bar(g)   | 흡입 압력                                |       1 | —    | —    |
| `P-1101A` | `PD-P-1101A` | discharge_pressure | bar(g)   | 토출 압력                                |       1 | —    | —    |
| `P-1101A` | `FI-P-1101A` | flow               | m3/h     | 액체 체적 유량                           |       1 | —    | —    |
| `P-1101A` | `II-P-1101A` | current            | A        | 구동 모터 전류                           |       1 | —    | —    |
| `B-1201`  | `VX-B-1201`  | shaft_vibration    | um p-p   | 구동측 X축 상대 진동                     |       1 | —    | —    |
| `B-1201`  | `VY-B-1201`  | shaft_vibration    | um p-p   | 구동측 Y축 상대 진동                     |       1 | —    | —    |
| `B-1201`  | `TI-B-1201`  | bearing_temp       | degC     | 구동측 저널 베어링 온도                  |      10 | —    | —    |
| `B-1201`  | `PS-B-1201`  | suction_pressure   | bar(g)   | 입구 공기 압력                           |       1 | —    | —    |
| `B-1201`  | `PD-B-1201`  | discharge_pressure | bar(g)   | 토출 공기 압력                           |       1 | —    | —    |
| `B-1201`  | `FI-B-1201`  | flow               | Nm3/h    | 표준 상태 공기 체적 유량                 |       1 | —    | —    |
| `B-1201`  | `SI-B-1201`  | speed              | rpm      | 축 회전속도                              |       1 | —    | —    |
| `B-1201`  | `LOP-B-1201` | oil_pressure       | bar(g)   | 윤활유 공급 압력                         |       1 | —    | —    |
| `E-1202`  | `VX-E-1202`  | shaft_vibration    | um p-p   | 구동측 X축 상대 진동                     |       1 | —    | —    |
| `E-1202`  | `VY-E-1202`  | shaft_vibration    | um p-p   | 구동측 Y축 상대 진동                     |       1 | —    | —    |
| `E-1202`  | `TI-E-1202`  | bearing_temp       | degC     | 저널 베어링 온도                         |      10 | —    | —    |
| `E-1202`  | `ZI-E-1202`  | axial_position     | mm       | 축방향 상대 위치                         |       1 | —    | —    |
| `E-1202`  | `SI-E-1202`  | speed              | rpm      | 축 회전속도                              |       1 | —    | —    |
| `E-1202`  | `PS-E-1202`  | suction_pressure   | bar(g)   | 배기가스 입구 압력                       |       1 | —    | —    |
| `E-1202`  | `PD-E-1202`  | discharge_pressure | bar(g)   | 배기가스 출구 압력                       |       1 | —    | —    |
| `E-1202`  | `LOT-E-1202` | oil_temp           | degC     | 윤활유 공급 온도                         |      10 | —    | —    |
| `P-1301A` | `VI-P-1301A` | vibration          | mm/s RMS | 펌프 구동측 케이싱 진동 속도 Overall RMS |       1 | —    | —    |
| `P-1301A` | `TI-P-1301A` | bearing_temp       | degC     | 펌프 구동측 베어링 온도                  |      10 | —    | —    |
| `P-1301A` | `PS-P-1301A` | suction_pressure   | bar(g)   | 흡입 압력                                |       1 | —    | —    |
| `P-1301A` | `PD-P-1301A` | discharge_pressure | bar(g)   | 토출 압력                                |       1 | —    | —    |
| `P-1301A` | `FI-P-1301A` | flow               | m3/h     | 액체 체적 유량                           |       1 | —    | —    |
| `P-1301A` | `II-P-1301A` | current            | A        | 구동 모터 전류                           |       1 | —    | —    |
| `P-1302A` | `VI-P-1302A` | vibration          | mm/s RMS | 펌프 구동측 케이싱 진동 속도 Overall RMS |       1 | —    | —    |
| `P-1302A` | `TI-P-1302A` | bearing_temp       | degC     | 펌프 구동측 베어링 온도                  |      10 | —    | —    |
| `P-1302A` | `PS-P-1302A` | suction_pressure   | bar(g)   | 흡입 압력                                |       1 | —    | —    |
| `P-1302A` | `PD-P-1302A` | discharge_pressure | bar(g)   | 토출 압력                                |       1 | —    | —    |
| `P-1302A` | `FI-P-1302A` | flow               | m3/h     | 액체 체적 유량                           |       1 | —    | —    |
| `P-1302A` | `II-P-1302A` | current            | A        | 구동 모터 전류                           |       1 | —    | —    |
| `P-1303A` | `VI-P-1303A` | vibration          | mm/s RMS | 펌프 구동측 케이싱 진동 속도 Overall RMS |       1 | —    | —    |
| `P-1303A` | `TI-P-1303A` | bearing_temp       | degC     | 펌프 구동측 베어링 온도                  |      10 | —    | —    |
| `P-1303A` | `PS-P-1303A` | suction_pressure   | bar(g)   | 흡입 압력                                |       1 | —    | —    |
| `P-1303A` | `PD-P-1303A` | discharge_pressure | bar(g)   | 토출 압력                                |       1 | —    | —    |
| `P-1303A` | `FI-P-1303A` | flow               | m3/h     | 액체 체적 유량                           |       1 | —    | —    |
| `P-1303A` | `II-P-1303A` | current            | A        | 구동 모터 전류                           |       1 | —    | —    |
| `C-1401`  | `VX-C-1401`  | shaft_vibration    | um p-p   | 구동측 X축 상대 진동                     |       1 | —    | —    |
| `C-1401`  | `VY-C-1401`  | shaft_vibration    | um p-p   | 구동측 Y축 상대 진동                     |       1 | —    | —    |
| `C-1401`  | `TI-C-1401`  | bearing_temp       | degC     | 구동측 저널 베어링 온도                  |      10 | —    | —    |
| `C-1401`  | `ZI-C-1401`  | axial_position     | mm       | 축방향 상대 위치                         |       1 | —    | —    |
| `C-1401`  | `PS-C-1401`  | suction_pressure   | bar(g)   | 1단 흡입 압력                            |       1 | —    | —    |
| `C-1401`  | `PD-C-1401`  | discharge_pressure | bar(g)   | 최종 토출 압력                           |       1 | —    | —    |
| `C-1401`  | `TS-C-1401`  | suction_temp       | degC     | 1단 흡입 온도                            |      10 | —    | —    |
| `C-1401`  | `TD-C-1401`  | discharge_temp     | degC     | 최종 토출 온도                           |      10 | —    | —    |
| `C-1401`  | `FI-C-1401`  | flow               | Nm3/h    | 흡입 가스 표준 상태 체적 유량            |       1 | —    | —    |
| `C-1401`  | `SI-C-1401`  | speed              | rpm      | 축 회전속도                              |       1 | —    | —    |
| `T-1401`  | `VX-T-1401`  | shaft_vibration    | um p-p   | 구동측 X축 상대 진동                     |       1 | —    | —    |
| `T-1401`  | `VY-T-1401`  | shaft_vibration    | um p-p   | 구동측 Y축 상대 진동                     |       1 | —    | —    |
| `T-1401`  | `TI-T-1401`  | bearing_temp       | degC     | 구동측 저널 베어링 온도                  |      10 | —    | —    |
| `T-1401`  | `SI-T-1401`  | speed              | rpm      | 축 회전속도                              |       1 | —    | —    |
| `T-1401`  | `LOP-T-1401` | oil_pressure       | bar(g)   | 윤활유 공급 압력                         |       1 | —    | —    |
| `T-1401`  | `LOT-T-1401` | oil_temp           | degC     | 윤활유 공급 온도                         |      10 | —    | —    |
| `T-1401`  | `SP-T-1401`  | steam_pressure     | bar(g)   | 입구 증기 압력                           |       1 | —    | —    |
| `P-1402A` | `VI-P-1402A` | vibration          | mm/s RMS | 펌프 구동측 케이싱 진동 속도 Overall RMS |       1 | —    | —    |
| `P-1402A` | `TI-P-1402A` | bearing_temp       | degC     | 펌프 구동측 베어링 온도                  |      10 | —    | —    |
| `P-1402A` | `PS-P-1402A` | suction_pressure   | bar(g)   | 흡입 압력                                |       1 | —    | —    |
| `P-1402A` | `PD-P-1402A` | discharge_pressure | bar(g)   | 토출 압력                                |       1 | —    | —    |
| `P-1402A` | `FI-P-1402A` | flow               | m3/h     | 액체 체적 유량                           |       1 | —    | —    |
| `P-1402A` | `II-P-1402A` | current            | A        | 구동 모터 전류                           |       1 | —    | —    |
| `P-1403A` | `VI-P-1403A` | vibration          | mm/s RMS | 펌프 구동측 케이싱 진동 속도 Overall RMS |       1 | —    | —    |
| `P-1403A` | `TI-P-1403A` | bearing_temp       | degC     | 펌프 구동측 베어링 온도                  |      10 | —    | —    |
| `P-1403A` | `PS-P-1403A` | suction_pressure   | bar(g)   | 흡입 압력                                |       1 | —    | —    |
| `P-1403A` | `PD-P-1403A` | discharge_pressure | bar(g)   | 토출 압력                                |       1 | —    | —    |
| `P-1403A` | `FI-P-1403A` | flow               | m3/h     | 액체 체적 유량                           |       1 | —    | —    |
| `P-1403A` | `II-P-1403A` | current            | A        | 구동 모터 전류                           |       1 | —    | —    |
| `P-1501A` | `VI-P-1501A` | vibration          | mm/s RMS | 펌프 구동측 케이싱 진동 속도 Overall RMS |       1 | —    | —    |
| `P-1501A` | `TI-P-1501A` | bearing_temp       | degC     | 펌프 구동측 베어링 온도                  |      10 | —    | —    |
| `P-1501A` | `PS-P-1501A` | suction_pressure   | bar(g)   | 흡입 압력                                |       1 | —    | —    |
| `P-1501A` | `PD-P-1501A` | discharge_pressure | bar(g)   | 토출 압력                                |       1 | —    | —    |
| `P-1501A` | `FI-P-1501A` | flow               | m3/h     | 액체 체적 유량                           |       1 | —    | —    |
| `P-1501A` | `II-P-1501A` | current            | A        | 구동 모터 전류                           |       1 | —    | —    |
| `P-1502A` | `VI-P-1502A` | vibration          | mm/s RMS | 펌프 구동측 케이싱 진동 속도 Overall RMS |       1 | —    | —    |
| `P-1502A` | `TI-P-1502A` | bearing_temp       | degC     | 펌프 구동측 베어링 온도                  |      10 | —    | —    |
| `P-1502A` | `PS-P-1502A` | suction_pressure   | bar(g)   | 흡입 압력                                |       1 | —    | —    |
| `P-1502A` | `PD-P-1502A` | discharge_pressure | bar(g)   | 토출 압력                                |       1 | —    | —    |
| `P-1502A` | `FI-P-1502A` | flow               | m3/h     | 액체 체적 유량                           |       1 | —    | —    |
| `P-1502A` | `II-P-1502A` | current            | A        | 구동 모터 전류                           |       1 | —    | —    |

</details>

## 2. CDU (상압증류) — `cdu` (`PL-CDU`)

설비 7대 · 태그 46개

| 설비      | 이름                                                   | 유형    | 공정 그룹 / 단위    | 태그 수 |
| --------- | ------------------------------------------------------ | ------- | ------------------- | ------: |
| `P-8101A` | 원유 공급 펌프 A (Crude Charge Pump A)                 | PM 펌프 | 원유 전처리 / U-811 |       7 |
| `P-8102A` | 탈염 세정수 펌프 A (Desalter Wash Water Pump A)        | PM 펌프 | 원유 전처리 / U-812 |       6 |
| `F-8201`  | 가열로 강제 통풍 팬 (Crude Heater FD Fan)              | FN 팬   | 원유 가열 / U-821   |       7 |
| `P-8301A` | 중간 순환환류 펌프 A (Middle Pumparound Pump A)        | PM 펌프 | 상압증류 / U-831    |       7 |
| `P-8302A` | 탑정 환류 펌프 A (Overhead Reflux Pump A)              | PM 펌프 | 상압증류 / U-832    |       6 |
| `P-8303A` | 상압탑 하부 펌프 A (Atmospheric Bottoms Pump A)        | PM 펌프 | 상압증류 / U-833    |       7 |
| `P-8401A` | 경유 제품 이송 펌프 A (Diesel Product Transfer Pump A) | PM 펌프 | 제품 회수 / U-841   |       6 |

<details><summary>태그 목록 (한계값: 경고 / 알람 / 정지)</summary>

| 설비      | 태그        | 신호                  | 단위      | 설명                | 주기(s) | 하한              | 상한            |
| --------- | ----------- | --------------------- | --------- | ------------------- | ------: | ----------------- | --------------- |
| `P-8101A` | `VI-8101A`  | vibration             | mm/s RMS  | 펌프 구동측 진동    |       1 | —                 | 3 / 4.5 / 7.1   |
| `P-8101A` | `VI-8101B`  | vibration             | mm/s RMS  | 펌프 반구동측 진동  |       1 | —                 | 3 / 4.5 / 7.1   |
| `P-8101A` | `TI-8101A`  | bearing_temp          | DEG C     | 펌프 베어링 온도    |      10 | —                 | 80 / 90 / 100   |
| `P-8101A` | `PI-8101A`  | suction_pressure      | kg/cm2(g) | 펌프 흡입 압력      |       1 | 2.02 / 1.62 / —   | —               |
| `P-8101A` | `PI-8101B`  | discharge_pressure    | kg/cm2(g) | 펌프 토출 압력      |       1 | 20.16 / 16.24 / — | —               |
| `P-8101A` | `FI-8101A`  | flow                  | m3/h      | 원유 공급 유량      |       1 | 288 / 248 / —     | —               |
| `P-8101A` | `II-8101A`  | current               | A         | 모터 전류           |       1 | —                 | 122 / 129 / —   |
| `P-8102A` | `VI-8102A`  | vibration             | mm/s RMS  | 펌프 구동측 진동    |       1 | —                 | 3 / 4.5 / 7.1   |
| `P-8102A` | `VI-8102B`  | vibration             | mm/s RMS  | 펌프 반구동측 진동  |       1 | —                 | 3 / 4.5 / 7.1   |
| `P-8102A` | `TI-8102A`  | bearing_temp          | DEG C     | 펌프 베어링 온도    |      10 | —                 | 80 / 90 / 100   |
| `P-8102A` | `PI-8102B`  | discharge_pressure    | kg/cm2(g) | 펌프 토출 압력      |       1 | 12.6 / 10.15 / —  | —               |
| `P-8102A` | `FI-8102A`  | flow                  | m3/h      | 세정수 유량         |       1 | 29 / 25 / —       | —               |
| `P-8102A` | `II-8102A`  | current               | A         | 모터 전류           |       1 | —                 | 174 / 184 / —   |
| `F-8201`  | `VI-8201A`  | vibration             | mm/s RMS  | 팬 구동측 진동      |       1 | —                 | 4.5 / 7.1 / 11  |
| `F-8201`  | `VI-8201B`  | vibration             | mm/s RMS  | 팬 반구동측 진동    |       1 | —                 | 4.5 / 7.1 / 11  |
| `F-8201`  | `TI-8201A`  | bearing_temp          | DEG C     | 팬 베어링 온도      |      10 | —                 | 80 / 90 / 100   |
| `F-8201`  | `SI-8201A`  | speed                 | rpm       | 회전속도            |       1 | —                 | 1250 / 1300 / — |
| `F-8201`  | `II-8201A`  | current               | A         | 모터 전류           |       1 | —                 | 53 / 57 / —     |
| `F-8201`  | `FI-8201A`  | flow                  | Nm3/h     | 연소 공기 유량      |       1 | 85000 / 75000 / — | —               |
| `F-8201`  | `PDI-8201A` | differential_pressure | mmH2O     | 팬 전후 차압        |       1 | 140 / 120 / —     | 240 / — / —     |
| `P-8301A` | `VI-8301A`  | vibration             | mm/s RMS  | 펌프 구동측 진동    |       1 | —                 | 3 / 4.5 / 7.1   |
| `P-8301A` | `VI-8301B`  | vibration             | mm/s RMS  | 펌프 반구동측 진동  |       1 | —                 | 3 / 4.5 / 7.1   |
| `P-8301A` | `TI-8301A`  | bearing_temp          | DEG C     | 펌프 베어링 온도    |      10 | —                 | 80 / 90 / 100   |
| `P-8301A` | `FI-8301A`  | flow                  | m3/h      | 중간 순환환류 유량  |       1 | 374 / 322 / —     | —               |
| `P-8301A` | `PI-8301A`  | suction_pressure      | kg/cm2(g) | 펌프 흡입 압력      |       1 | 1.87 / 1.51 / —   | —               |
| `P-8301A` | `PI-8301B`  | discharge_pressure    | kg/cm2(g) | 펌프 토출 압력      |       1 | 6.48 / 5.22 / —   | —               |
| `P-8301A` | `TI-8301B`  | process_temp          | DEG C     | 인출 유체 온도      |      10 | —                 | 235 / 245 / —   |
| `P-8302A` | `VI-8302A`  | vibration             | mm/s RMS  | 펌프 구동측 진동    |       1 | —                 | 3 / 4.5 / 7.1   |
| `P-8302A` | `VI-8302B`  | vibration             | mm/s RMS  | 펌프 반구동측 진동  |       1 | —                 | 3 / 4.5 / 7.1   |
| `P-8302A` | `TI-8302A`  | bearing_temp          | DEG C     | 펌프 베어링 온도    |      10 | —                 | 80 / 90 / 100   |
| `P-8302A` | `PI-8302A`  | suction_pressure      | kg/cm2(g) | 펌프 흡입 압력      |       1 | 0.68 / 0.55 / —   | —               |
| `P-8302A` | `PI-8302B`  | discharge_pressure    | kg/cm2(g) | 펌프 토출 압력      |       1 | 4.68 / 3.77 / —   | —               |
| `P-8302A` | `FI-8302A`  | flow                  | m3/h      | 탑정 환류 유량      |       1 | 216 / 186 / —     | —               |
| `P-8303A` | `VI-8303A`  | vibration             | mm/s RMS  | 펌프 구동측 진동    |       1 | —                 | 3 / 4.5 / 7.1   |
| `P-8303A` | `VI-8303B`  | vibration             | mm/s RMS  | 펌프 반구동측 진동  |       1 | —                 | 3 / 4.5 / 7.1   |
| `P-8303A` | `TI-8303A`  | bearing_temp          | DEG C     | 펌프 베어링 온도    |      10 | —                 | 80 / 90 / 100   |
| `P-8303A` | `TI-8303B`  | process_temp          | DEG C     | 흡입 유체 온도      |      10 | —                 | 360 / 370 / —   |
| `P-8303A` | `PI-8303A`  | suction_pressure      | kg/cm2(g) | 펌프 흡입 압력      |       1 | 1.51 / 1.22 / —   | —               |
| `P-8303A` | `PI-8303B`  | discharge_pressure    | kg/cm2(g) | 펌프 토출 압력      |       1 | 10.08 / 8.12 / —  | —               |
| `P-8303A` | `FI-8303A`  | flow                  | m3/h      | 상압 잔사유 유량    |       1 | 259 / 223 / —     | —               |
| `P-8401A` | `VI-8401A`  | vibration             | mm/s RMS  | 펌프 구동측 진동    |       1 | —                 | 3 / 4.5 / 7.1   |
| `P-8401A` | `VI-8401B`  | vibration             | mm/s RMS  | 펌프 반구동측 진동  |       1 | —                 | 3 / 4.5 / 7.1   |
| `P-8401A` | `TI-8401A`  | bearing_temp          | DEG C     | 펌프 베어링 온도    |      10 | —                 | 80 / 90 / 100   |
| `P-8401A` | `FI-8401A`  | flow                  | m3/h      | 경유 제품 이송 유량 |       1 | 151 / 130 / —     | —               |
| `P-8401A` | `PI-8401B`  | discharge_pressure    | kg/cm2(g) | 펌프 토출 압력      |       1 | 7.56 / 6.09 / —   | —               |
| `P-8401A` | `II-8401A`  | current               | A         | 모터 전류           |       1 | —                 | 46 / 49 / —     |

</details>

## 3. HOU (수첨분해) — `hou` (`PL-HOU`)

설비 7대 · 태그 61개

| 설비      | 이름                                              | 유형      | 공정 그룹 / 단위             | 태그 수 |
| --------- | ------------------------------------------------- | --------- | ---------------------------- | ------: |
| `P-2101`  | 고압 원료 공급 펌프 (High Pressure Feed Pump)     | PM 펌프   | 원료 공급 / 원료 가압·공급   |       8 |
| `C-2201`  | 보충 수소 압축기 (Make-up Hydrogen Compressor)    | CC 압축기 | 수소 공급 / 보충 수소 압축   |       9 |
| `C-2301`  | 수소 순환 압축기 (Recycle Gas Compressor)         | CC 압축기 | 수첨반응 / 수소 순환         |      13 |
| `FN-2401` | 반응기 가열로 연소공기·배기 팬 (FD/ID Fan)        | FN 팬     | 수첨반응 / 반응기 가열       |       8 |
| `P-2501`  | 압축기 윤활유 펌프 (Lube Oil Pump)                | PM 펌프   | 기액 분리·순환 / 압축기 지원 |       6 |
| `P-2601`  | 분별탑 환류 펌프 (Reflux Pump)                    | PM 펌프   | 생성물 분별 / 분별탑 환류    |       8 |
| `P-2701`  | 미전환유 순환 펌프 (Unconverted Oil Recycle Pump) | PM 펌프   | 생성물 분별 / 미전환유 순환  |       9 |

<details><summary>태그 목록 (한계값: 경고 / 알람 / 정지)</summary>

| 설비      | 태그       | 신호                  | 단위      | 설명                           | 주기(s) | 하한                | 상한            |
| --------- | ---------- | --------------------- | --------- | ------------------------------ | ------: | ------------------- | --------------- |
| `P-2101`  | `VI-2101A` | vibration             | mm/s RMS  | 펌프 구동측 진동               |       1 | —                   | 3.5 / 4.5 / 7.1 |
| `P-2101`  | `VI-2101B` | vibration             | mm/s RMS  | 펌프 반구동측 진동             |       1 | —                   | 3.5 / 4.5 / 7.1 |
| `P-2101`  | `TI-2101A` | bearing_temp          | DEG C     | 펌프 구동측 베어링 온도        |      10 | —                   | 80 / 90 / 100   |
| `P-2101`  | `TI-2101B` | bearing_temp          | DEG C     | 펌프 반구동측 베어링 온도      |      10 | —                   | 80 / 90 / 100   |
| `P-2101`  | `PI-2101A` | suction_pressure      | kg/cm2(g) | 펌프 흡입압력                  |       1 | — / 4.5 / 3.5       | — / 9 / —       |
| `P-2101`  | `PI-2101B` | discharge_pressure    | kg/cm2(g) | 펌프 토출압력                  |       1 | — / 170 / —         | — / 198 / 205   |
| `P-2101`  | `FI-2101`  | flow                  | m3/h      | 펌프 토출 유량                 |       1 | — / 130 / 100       | — / 215 / —     |
| `P-2101`  | `II-2101`  | current               | A         | 모터 전류                      |       1 | —                   | — / 195 / 210   |
| `C-2201`  | `VI-2201A` | vibration             | mm/s RMS  | 크랭크 프레임 진동             |       1 | —                   | 8 / 11 / 18     |
| `C-2201`  | `VI-2201B` | vibration             | mm/s RMS  | 실린더 진동                    |       1 | —                   | 8.5 / 11 / 18   |
| `C-2201`  | `TI-2201A` | bearing_temp          | DEG C     | 메인 베어링 온도               |      10 | —                   | 85 / 95 / 105   |
| `C-2201`  | `TI-2201B` | suction_temp          | DEG C     | 흡입 온도                      |      10 | —                   | — / 55 / —      |
| `C-2201`  | `TI-2201C` | discharge_temp        | DEG C     | 토출 온도                      |      10 | —                   | 135 / 145 / 160 |
| `C-2201`  | `PI-2201A` | suction_pressure      | kg/cm2(g) | 흡입압력                       |       1 | — / 19 / 16         | — / 28 / —      |
| `C-2201`  | `PI-2201B` | discharge_pressure    | kg/cm2(g) | 토출압력                       |       1 | — / 150 / —         | — / 178 / 185   |
| `C-2201`  | `FI-2201`  | flow                  | Nm3/h     | 토출 가스 유량                 |       1 | — / 26000 / 20000   | — / 46000 / —   |
| `C-2201`  | `SI-2201`  | speed                 | rpm       | 압축기 회전속도                |       1 | — / 300 / —         | — / 360 / 375   |
| `C-2301`  | `VI-2301A` | shaft_vibration       | um pk-pk  | 구동측 축 진동 (X)             |       1 | —                   | 22 / 28 / 38    |
| `C-2301`  | `VI-2301B` | shaft_vibration       | um pk-pk  | 구동측 축 진동 (Y)             |       1 | —                   | 22 / 28 / 38    |
| `C-2301`  | `VI-2301C` | shaft_vibration       | um pk-pk  | 반구동측 축 진동 (X)           |       1 | —                   | 22 / 28 / 38    |
| `C-2301`  | `VI-2301D` | shaft_vibration       | um pk-pk  | 반구동측 축 진동 (Y)           |       1 | —                   | 22 / 28 / 38    |
| `C-2301`  | `TI-2301A` | bearing_temp          | DEG C     | 구동측 저널 베어링 패드 온도   |      10 | —                   | 100 / 115 / 125 |
| `C-2301`  | `TI-2301B` | bearing_temp          | DEG C     | 반구동측 저널 베어링 패드 온도 |      10 | —                   | 100 / 115 / 125 |
| `C-2301`  | `ZI-2301`  | axial_position        | mm        | 축방향 위치                    |       1 | — / -0.3 / -0.5     | 0.2 / 0.3 / 0.5 |
| `C-2301`  | `PI-2301A` | suction_pressure      | kg/cm2(g) | 흡입압력                       |       1 | — / 115 / —         | — / 140 / —     |
| `C-2301`  | `PI-2301B` | discharge_pressure    | kg/cm2(g) | 토출압력                       |       1 | — / 128 / —         | — / 155 / —     |
| `C-2301`  | `TI-2301C` | suction_temp          | DEG C     | 흡입 온도                      |      10 | —                   | — / 70 / —      |
| `C-2301`  | `TI-2301D` | discharge_temp        | DEG C     | 토출 온도                      |      10 | —                   | 95 / 105 / 115  |
| `C-2301`  | `FI-2301`  | flow                  | Nm3/h     | 순환 가스 유량                 |       1 | — / 215000 / 190000 | — / 330000 / —  |
| `C-2301`  | `SI-2301`  | speed                 | rpm       | 회전속도                       |       1 | — / 4600 / —        | — / 5517 / 5700 |
| `FN-2401` | `VI-2401A` | vibration             | mm/s RMS  | 팬 구동측 진동                 |       1 | —                   | 3.5 / 4.5 / 7.1 |
| `FN-2401` | `VI-2401B` | vibration             | mm/s RMS  | 팬 반구동측 진동               |       1 | —                   | 3.5 / 4.5 / 7.1 |
| `FN-2401` | `TI-2401A` | bearing_temp          | DEG C     | 팬 구동측 베어링 온도          |      10 | —                   | 75 / 85 / 95    |
| `FN-2401` | `TI-2401B` | bearing_temp          | DEG C     | 팬 반구동측 베어링 온도        |      10 | —                   | 75 / 85 / 95    |
| `FN-2401` | `II-2401`  | current               | A         | 모터 전류                      |       1 | —                   | — / 98 / 108    |
| `FN-2401` | `SI-2401`  | speed                 | rpm       | 팬 회전속도                    |       1 | — / 1600 / —        | — / 1830 / 1860 |
| `FN-2401` | `FI-2401`  | flow                  | Nm3/h     | 공기 유량                      |       1 | — / 45000 / 38000   | — / 72000 / —   |
| `FN-2401` | `PDI-2401` | differential_pressure | mmH2O     | 팬 차압                        |       1 | — / 140 / —         | — / 230 / —     |
| `P-2501`  | `VI-2501`  | vibration             | mm/s RMS  | 펌프 진동                      |       1 | —                   | 3.5 / 4.5 / 7.1 |
| `P-2501`  | `TI-2501A` | bearing_temp          | DEG C     | 펌프 베어링 온도               |      10 | —                   | 80 / 90 / 100   |
| `P-2501`  | `PI-2501`  | oil_pressure          | kg/cm2(g) | 윤활유 토출압력                |       1 | — / 6 / 5           | — / 9.5 / —     |
| `P-2501`  | `TI-2501B` | oil_temp              | DEG C     | 윤활유 온도                    |      10 | —                   | 58 / 65 / 75    |
| `P-2501`  | `FI-2501`  | flow                  | m3/h      | 윤활유 유량                    |       1 | — / 30 / 24         | — / 52 / —      |
| `P-2501`  | `II-2501`  | current               | A         | 모터 전류                      |       1 | —                   | — / 30 / 34     |
| `P-2601`  | `VI-2601A` | vibration             | mm/s RMS  | 펌프 구동측 진동               |       1 | —                   | 3.5 / 4.5 / 7.1 |
| `P-2601`  | `VI-2601B` | vibration             | mm/s RMS  | 펌프 반구동측 진동             |       1 | —                   | 3.5 / 4.5 / 7.1 |
| `P-2601`  | `TI-2601A` | bearing_temp          | DEG C     | 펌프 구동측 베어링 온도        |      10 | —                   | 80 / 90 / 100   |
| `P-2601`  | `TI-2601B` | bearing_temp          | DEG C     | 펌프 반구동측 베어링 온도      |      10 | —                   | 80 / 90 / 100   |
| `P-2601`  | `PI-2601A` | suction_pressure      | kg/cm2(g) | 펌프 흡입압력                  |       1 | — / 2.5 / 1.5       | — / 6 / —       |
| `P-2601`  | `PI-2601B` | discharge_pressure    | kg/cm2(g) | 펌프 토출압력                  |       1 | — / 7.5 / 6         | — / 12 / —      |
| `P-2601`  | `FI-2601`  | flow                  | m3/h      | 환류 유량                      |       1 | — / 55 / 40         | — / 120 / —     |
| `P-2601`  | `II-2601`  | current               | A         | 모터 전류                      |       1 | —                   | — / 56 / 62     |
| `P-2701`  | `VI-2701A` | vibration             | mm/s RMS  | 펌프 구동측 진동               |       1 | —                   | 3.5 / 4.5 / 7.1 |
| `P-2701`  | `VI-2701B` | vibration             | mm/s RMS  | 펌프 반구동측 진동             |       1 | —                   | 3.5 / 4.5 / 7.1 |
| `P-2701`  | `TI-2701A` | bearing_temp          | DEG C     | 펌프 구동측 베어링 온도        |      10 | —                   | 82 / 90 / 100   |
| `P-2701`  | `TI-2701B` | bearing_temp          | DEG C     | 펌프 반구동측 베어링 온도      |      10 | —                   | 82 / 90 / 100   |
| `P-2701`  | `TI-2701C` | process_temp          | DEG C     | 취급 유체 온도                 |      10 | —                   | — / 330 / 345   |
| `P-2701`  | `PI-2701A` | suction_pressure      | kg/cm2(g) | 펌프 흡입압력                  |       1 | — / 2.2 / 1.4       | — / 5.5 / —     |
| `P-2701`  | `PI-2701B` | discharge_pressure    | kg/cm2(g) | 펌프 토출압력                  |       1 | — / 26 / 22         | — / 38 / —      |
| `P-2701`  | `FI-2701`  | flow                  | m3/h      | 순환 유량                      |       1 | — / 95 / 75         | — / 170 / —     |
| `P-2701`  | `II-2701`  | current               | A         | 모터 전류                      |       1 | —                   | — / 118 / 128   |

</details>

## 4. RHDS (상압 잔사유 수첨탈황) — `rhds` (`PL-RHDS`)

설비 6대 · 태그 43개

| 설비     | 이름                                           | 유형      | 공정 그룹 / 단위             | 태그 수 |
| -------- | ---------------------------------------------- | --------- | ---------------------------- | ------: |
| `P-3101` | 고압 원료 공급 펌프 (High Pressure Feed Pump)  | PM 펌프   | 원료 공급 / 잔사유 가압·공급 |       6 |
| `C-3102` | 보충 수소 압축기 (Make-up Hydrogen Compressor) | CC 압축기 | 수소 공급 / 보충 수소 압축   |       8 |
| `C-3101` | 순환가스 압축기 (Recycle Gas Compressor)       | CC 압축기 | 수첨탈황 반응 / 수소 순환    |      11 |
| `F-3101` | 가열로 강제통풍 팬 (FD Fan)                    | FN 팬     | 반응 가열 / 연소공기 공급    |       6 |
| `P-3102` | 고온 잔사유 이송 펌프 (Hot Residue Pump)       | PM 펌프   | 고온 유체 처리 / 잔사유 이송 |       7 |
| `P-3103` | 분별탑 환류 펌프 (Fractionator Reflux Pump)    | PM 펌프   | 생성물 분별 / 환류·순환      |       5 |

<details><summary>태그 목록 (한계값: 경고 / 알람 / 정지)</summary>

| 설비     | 태그          | 신호                  | 단위     | 설명                      | 주기(s) | 하한           | 상한          |
| -------- | ------------- | --------------------- | -------- | ------------------------- | ------: | -------------- | ------------- |
| `P-3101` | `VI-A-P3101`  | vibration             | mm/s RMS | 구동측 수평 케이싱 진동   |       1 | —              | 4.5 / — / —   |
| `P-3101` | `TI-B-P3101`  | bearing_temp          | DEG C    | 구동측 베어링 온도        |      10 | —              | 85 / — / —    |
| `P-3101` | `PI-S-P3101`  | suction_pressure      | bar(g)   | 흡입 압력                 |       1 | 3.5 / — / —    | —             |
| `P-3101` | `PI-D-P3101`  | discharge_pressure    | bar(g)   | 토출 압력                 |       1 | —              | 170 / — / —   |
| `P-3101` | `FI-P3101`    | flow                  | m3/h     | 원료 유량                 |       1 | 160 / — / —    | —             |
| `P-3101` | `II-P3101`    | current               | A        | 모터 전류                 |       1 | —              | 210 / — / —   |
| `C-3102` | `VI-A-C3102`  | vibration             | mm/s RMS | 프레임 케이싱 진동        |       1 | —              | 6 / — / —     |
| `C-3102` | `TI-B-C3102`  | bearing_temp          | DEG C    | 프레임 베어링 온도        |      10 | —              | 85 / — / —    |
| `C-3102` | `PI-S-C3102`  | suction_pressure      | bar(g)   | 첫 단 흡입 압력           |       1 | 20 / — / —     | —             |
| `C-3102` | `PI-D-C3102`  | discharge_pressure    | bar(g)   | 최종 단 토출 압력         |       1 | —              | 175 / — / —   |
| `C-3102` | `TI-S-C3102`  | suction_temp          | DEG C    | 첫 단 흡입 가스 온도      |      10 | —              | 50 / — / —    |
| `C-3102` | `TI-D-C3102`  | discharge_temp        | DEG C    | 최종 단 토출 가스 온도    |      10 | —              | 145 / — / —   |
| `C-3102` | `FI-C3102`    | flow                  | Nm3/h    | 보충 수소 표준 유량       |       1 | 9000 / — / —   | —             |
| `C-3102` | `SI-C3102`    | speed                 | rpm      | 크랭크축 회전속도         |       1 | 570 / — / —    | 610 / — / —   |
| `C-3101` | `XI-X-C3101`  | shaft_vibration       | um pk-pk | 구동측 X방향 축 상대 진동 |       1 | —              | 75 / — / —    |
| `C-3101` | `TI-B-C3101`  | bearing_temp          | DEG C    | 저널 베어링 금속 온도     |      10 | —              | 95 / — / —    |
| `C-3101` | `ZI-C3101`    | axial_position        | mm       | 축방향 위치·기준점 대비   |       1 | -0.35 / — / —  | 0.35 / — / —  |
| `C-3101` | `PI-S-C3101`  | suction_pressure      | bar(g)   | 흡입 압력                 |       1 | 125 / — / —    | —             |
| `C-3101` | `PI-D-C3101`  | discharge_pressure    | bar(g)   | 토출 압력                 |       1 | —              | 170 / — / —   |
| `C-3101` | `TI-S-C3101`  | suction_temp          | DEG C    | 흡입 가스 온도            |      10 | —              | 60 / — / —    |
| `C-3101` | `TI-D-C3101`  | discharge_temp        | DEG C    | 토출 가스 온도            |      10 | —              | 95 / — / —    |
| `C-3101` | `FI-C3101`    | flow                  | Nm3/h    | 순환가스 표준 유량        |       1 | 110000 / — / — | —             |
| `C-3101` | `SI-C3101`    | speed                 | rpm      | 압축기 회전속도           |       1 | 8500 / — / —   | 10000 / — / — |
| `C-3101` | `PI-LO-C3101` | oil_pressure          | bar(g)   | 윤활유 헤더 압력          |       1 | 2 / — / —      | —             |
| `C-3101` | `TI-LO-C3101` | oil_temp              | DEG C    | 윤활유 공급 온도          |      10 | —              | 55 / — / —    |
| `F-3101` | `VI-A-F3101`  | vibration             | mm/s RMS | 팬 구동측 케이싱 진동     |       1 | —              | 4.5 / — / —   |
| `F-3101` | `TI-B-F3101`  | bearing_temp          | DEG C    | 팬 구동측 베어링 온도     |      10 | —              | 80 / — / —    |
| `F-3101` | `SI-F3101`    | speed                 | rpm      | 팬 회전속도               |       1 | 1150 / — / —   | 1550 / — / —  |
| `F-3101` | `II-F3101`    | current               | A        | 모터 전류                 |       1 | —              | 80 / — / —    |
| `F-3101` | `FI-F3101`    | flow                  | m3/h     | 연소공기 실제 체적 유량   |       1 | 35000 / — / —  | —             |
| `F-3101` | `PDI-F3101`   | differential_pressure | Pa       | 팬 입출구 차압            |       1 | 1400 / — / —   | —             |
| `P-3102` | `VI-A-P3102`  | vibration             | mm/s RMS | 구동측 케이싱 진동        |       1 | —              | 4.5 / — / —   |
| `P-3102` | `TI-B-P3102`  | bearing_temp          | DEG C    | 구동측 베어링 온도        |      10 | —              | 90 / — / —    |
| `P-3102` | `TI-F-P3102`  | process_temp          | DEG C    | 흡입 유체 온도            |      10 | —              | 310 / — / —   |
| `P-3102` | `PI-S-P3102`  | suction_pressure      | bar(g)   | 흡입 압력                 |       1 | 3 / — / —      | —             |
| `P-3102` | `PI-D-P3102`  | discharge_pressure    | bar(g)   | 토출 압력                 |       1 | —              | 25 / — / —    |
| `P-3102` | `FI-P3102`    | flow                  | m3/h     | 잔사유 유량               |       1 | 140 / — / —    | —             |
| `P-3102` | `II-P3102`    | current               | A        | 모터 전류                 |       1 | —              | 145 / — / —   |
| `P-3103` | `VI-A-P3103`  | vibration             | mm/s RMS | 구동측 케이싱 진동        |       1 | —              | 4.5 / — / —   |
| `P-3103` | `TI-B-P3103`  | bearing_temp          | DEG C    | 구동측 베어링 온도        |      10 | —              | 80 / — / —    |
| `P-3103` | `PI-S-P3103`  | suction_pressure      | bar(g)   | 흡입 압력                 |       1 | 2 / — / —      | —             |
| `P-3103` | `PI-D-P3103`  | discharge_pressure    | bar(g)   | 토출 압력                 |       1 | —              | 15 / — / —    |
| `P-3103` | `FI-P3103`    | flow                  | m3/h     | 환류액 유량               |       1 | 65 / — / —     | —             |

</details>

## 5. VRDS (감압 잔사유 수첨탈황) — `vrds` (`PL-VRDS`)

설비 6대 · 태그 43개

| 설비     | 이름                                           | 유형      | 공정 그룹 / 단위             | 태그 수 |
| -------- | ---------------------------------------------- | --------- | ---------------------------- | ------: |
| `P-4101` | 고압 원료 공급 펌프 (High Pressure Feed Pump)  | PM 펌프   | 원료 공급 / 잔사유 가압·공급 |       6 |
| `C-4102` | 보충 수소 압축기 (Make-up Hydrogen Compressor) | CC 압축기 | 수소 공급 / 보충 수소 압축   |       8 |
| `C-4101` | 순환가스 압축기 (Recycle Gas Compressor)       | CC 압축기 | 수첨탈황 반응 / 수소 순환    |      11 |
| `F-4101` | 가열로 강제통풍 팬 (FD Fan)                    | FN 팬     | 반응 가열 / 연소공기 공급    |       6 |
| `P-4102` | 고온 잔사유 이송 펌프 (Hot Residue Pump)       | PM 펌프   | 고온 유체 처리 / 잔사유 이송 |       7 |
| `P-4103` | 분별탑 환류 펌프 (Fractionator Reflux Pump)    | PM 펌프   | 생성물 분별 / 환류·순환      |       5 |

<details><summary>태그 목록 (한계값: 경고 / 알람 / 정지)</summary>

| 설비     | 태그          | 신호                  | 단위     | 설명                      | 주기(s) | 하한           | 상한          |
| -------- | ------------- | --------------------- | -------- | ------------------------- | ------: | -------------- | ------------- |
| `P-4101` | `VI-A-P4101`  | vibration             | mm/s RMS | 구동측 수평 케이싱 진동   |       1 | —              | 4.5 / — / —   |
| `P-4101` | `TI-B-P4101`  | bearing_temp          | DEG C    | 구동측 베어링 온도        |      10 | —              | 85 / — / —    |
| `P-4101` | `PI-S-P4101`  | suction_pressure      | bar(g)   | 흡입 압력                 |       1 | 3.5 / — / —    | —             |
| `P-4101` | `PI-D-P4101`  | discharge_pressure    | bar(g)   | 토출 압력                 |       1 | —              | 170 / — / —   |
| `P-4101` | `FI-P4101`    | flow                  | m3/h     | 원료 유량                 |       1 | 160 / — / —    | —             |
| `P-4101` | `II-P4101`    | current               | A        | 모터 전류                 |       1 | —              | 210 / — / —   |
| `C-4102` | `VI-A-C4102`  | vibration             | mm/s RMS | 프레임 케이싱 진동        |       1 | —              | 6 / — / —     |
| `C-4102` | `TI-B-C4102`  | bearing_temp          | DEG C    | 프레임 베어링 온도        |      10 | —              | 85 / — / —    |
| `C-4102` | `PI-S-C4102`  | suction_pressure      | bar(g)   | 첫 단 흡입 압력           |       1 | 20 / — / —     | —             |
| `C-4102` | `PI-D-C4102`  | discharge_pressure    | bar(g)   | 최종 단 토출 압력         |       1 | —              | 175 / — / —   |
| `C-4102` | `TI-S-C4102`  | suction_temp          | DEG C    | 첫 단 흡입 가스 온도      |      10 | —              | 50 / — / —    |
| `C-4102` | `TI-D-C4102`  | discharge_temp        | DEG C    | 최종 단 토출 가스 온도    |      10 | —              | 145 / — / —   |
| `C-4102` | `FI-C4102`    | flow                  | Nm3/h    | 보충 수소 표준 유량       |       1 | 9000 / — / —   | —             |
| `C-4102` | `SI-C4102`    | speed                 | rpm      | 크랭크축 회전속도         |       1 | 570 / — / —    | 610 / — / —   |
| `C-4101` | `XI-X-C4101`  | shaft_vibration       | um pk-pk | 구동측 X방향 축 상대 진동 |       1 | —              | 75 / — / —    |
| `C-4101` | `TI-B-C4101`  | bearing_temp          | DEG C    | 저널 베어링 금속 온도     |      10 | —              | 95 / — / —    |
| `C-4101` | `ZI-C4101`    | axial_position        | mm       | 축방향 위치·기준점 대비   |       1 | -0.35 / — / —  | 0.35 / — / —  |
| `C-4101` | `PI-S-C4101`  | suction_pressure      | bar(g)   | 흡입 압력                 |       1 | 125 / — / —    | —             |
| `C-4101` | `PI-D-C4101`  | discharge_pressure    | bar(g)   | 토출 압력                 |       1 | —              | 170 / — / —   |
| `C-4101` | `TI-S-C4101`  | suction_temp          | DEG C    | 흡입 가스 온도            |      10 | —              | 60 / — / —    |
| `C-4101` | `TI-D-C4101`  | discharge_temp        | DEG C    | 토출 가스 온도            |      10 | —              | 95 / — / —    |
| `C-4101` | `FI-C4101`    | flow                  | Nm3/h    | 순환가스 표준 유량        |       1 | 110000 / — / — | —             |
| `C-4101` | `SI-C4101`    | speed                 | rpm      | 압축기 회전속도           |       1 | 8500 / — / —   | 10000 / — / — |
| `C-4101` | `PI-LO-C4101` | oil_pressure          | bar(g)   | 윤활유 헤더 압력          |       1 | 2 / — / —      | —             |
| `C-4101` | `TI-LO-C4101` | oil_temp              | DEG C    | 윤활유 공급 온도          |      10 | —              | 55 / — / —    |
| `F-4101` | `VI-A-F4101`  | vibration             | mm/s RMS | 팬 구동측 케이싱 진동     |       1 | —              | 4.5 / — / —   |
| `F-4101` | `TI-B-F4101`  | bearing_temp          | DEG C    | 팬 구동측 베어링 온도     |      10 | —              | 80 / — / —    |
| `F-4101` | `SI-F4101`    | speed                 | rpm      | 팬 회전속도               |       1 | 1150 / — / —   | 1550 / — / —  |
| `F-4101` | `II-F4101`    | current               | A        | 모터 전류                 |       1 | —              | 80 / — / —    |
| `F-4101` | `FI-F4101`    | flow                  | m3/h     | 연소공기 실제 체적 유량   |       1 | 35000 / — / —  | —             |
| `F-4101` | `PDI-F4101`   | differential_pressure | Pa       | 팬 입출구 차압            |       1 | 1400 / — / —   | —             |
| `P-4102` | `VI-A-P4102`  | vibration             | mm/s RMS | 구동측 케이싱 진동        |       1 | —              | 4.5 / — / —   |
| `P-4102` | `TI-B-P4102`  | bearing_temp          | DEG C    | 구동측 베어링 온도        |      10 | —              | 90 / — / —    |
| `P-4102` | `TI-F-P4102`  | process_temp          | DEG C    | 흡입 유체 온도            |      10 | —              | 310 / — / —   |
| `P-4102` | `PI-S-P4102`  | suction_pressure      | bar(g)   | 흡입 압력                 |       1 | 3 / — / —      | —             |
| `P-4102` | `PI-D-P4102`  | discharge_pressure    | bar(g)   | 토출 압력                 |       1 | —              | 25 / — / —    |
| `P-4102` | `FI-P4102`    | flow                  | m3/h     | 잔사유 유량               |       1 | 140 / — / —    | —             |
| `P-4102` | `II-P4102`    | current               | A        | 모터 전류                 |       1 | —              | 145 / — / —   |
| `P-4103` | `VI-A-P4103`  | vibration             | mm/s RMS | 구동측 케이싱 진동        |       1 | —              | 4.5 / — / —   |
| `P-4103` | `TI-B-P4103`  | bearing_temp          | DEG C    | 구동측 베어링 온도        |      10 | —              | 80 / — / —    |
| `P-4103` | `PI-S-P4103`  | suction_pressure      | bar(g)   | 흡입 압력                 |       1 | 2 / — / —      | —             |
| `P-4103` | `PI-D-P4103`  | discharge_pressure    | bar(g)   | 토출 압력                 |       1 | —              | 15 / — / —    |
| `P-4103` | `FI-P4103`    | flow                  | m3/h     | 환류액 유량               |       1 | 65 / — / —     | —             |

</details>
