# plant-simulator

Skadi 앞단 데이터 생성기. 가상 수첨탈황 공장(설비 20대, 태그 46개)의 센서 값을 끊임없이 만들어
중앙 수집 서버로 보내고, 수집 서버가 SQLite 파일 하나에 저장·집계·알람 판정한다.
기획서: [plant-simulator-plan.md](plant-simulator-plan.md)

```text
[config/plant_hds.yaml · runtime.yaml · scenario_dev.yaml]
        │
        ▼
[simulator]  ── HTTP POST /ingest ──▶  [collector (FastAPI)]  ──▶  data/plant.db (SQLite, WAL)
```

## 실행

```bash
uv sync                                  # 의존성 설치 (Python 3.11+)
uv run pytest                            # 테스트

# 터미널 1: 수집 서버
uv run python -m collector.app --scenario config/scenario_dev.yaml --db data/plant.db

# 터미널 2: 시뮬레이터 (backfill: 90일 백필 뒤 실시간으로 이어진다. 재시작하면 수집 서버의 마지막 시각부터 이어서 보낸다)
uv run python -m simulator.main --scenario config/scenario_dev.yaml --time-mode backfill
uv run python -m simulator.main --scenario config/scenario_dev.yaml                     # 실시간
uv run python -m simulator.main --scenario config/scenario_dev.yaml --time-mode accelerated

# Docker Compose 로 두 프로세스 함께
SIM_TIME_MODE=backfill docker compose up --build
```

평가용 시나리오(비공개)는 `--scenario` 를 한 번 더 주어 함께 적용한다. 수집 서버에는 개발용 파일만 준다.
Unit 단위로 나눠 돌리려면 `runtime.yaml` 의 `units: [U-1]`, `source_id: SIM-U1` 처럼 바꿔 프로세스를 여러 개 띄운다.

## API

| 메서드 · 경로 | 용도 |
| --- | --- |
| `POST /ingest` | 시뮬레이터 묶음 수신 |
| `GET /health` | 서버 상태, 마지막 수신 시각, 시뮬레이터 현재 시각 |
| `GET /tags/{tag_id}/current` | 태그 현재값 (전송 주기 3번 미수신이면 `NoData`) |
| `GET /timeseries?tag_id=&from=&to=&interval=1h` | 집계 시계열 (`10m` / `1h`) |
| `GET /alarms?equip_id=&from=&to=` | 알람 목록 |
| `GET /stream` | SSE 로 새 값 실시간 전달 |

DB 의 시각 컬럼은 모두 UTC ISO 문자열이다.

## 폴더

```text
config/      plant_hds.yaml(구성안) · runtime.yaml(실행 설정) · scenario_dev.yaml(개발용 시나리오)
common/      설정 모델 · 시나리오 모델 · 메시지 형식 v1 · 시뮬레이터 시계 · 시각 형식
simulator/   정상 신호 모델 · 이상 패턴 · 시나리오 적용 · 우연 알람 · 샘플 생성 · 전송 · 실행
collector/   FastAPI 앱 · 수집 규칙 · SQLite 저장 · 집계/알람/보관 배치 · 마스터/이력 적재 · SSE
tests/       단계별 완료 기준 테스트
```

구성안을 바꾸려면 `--plant` 에 다른 파일(예: `plant_hc.yaml`)을 주고, DB 를 비운 뒤 백필을 다시 돌린다.
