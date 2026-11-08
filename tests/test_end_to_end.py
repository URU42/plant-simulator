"""검증 (기획서 9.2 9단계): 시뮬레이터 → 수집 서버 경로로 백필하면 시나리오 구간에서 기대한 알람이 나온다."""
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from collector.app import create_app
from common.config_models import RuntimeConfig, load_plant
from common.message import IngestBatch
from common.sim_clock import KST, SimClock
from common.timefmt import to_db
from simulator.main import run_backfill_then_live
from simulator.scenario import load_scenarios

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=KST)   # EV-DEV-008 진행 중: VI-M4101 ramp 11-04 ~ 11-08 (1.7 → 3.2, 경고 2.8)


class ClientSender:
    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.results = []

    async def send(self, batch: IngestBatch):
        r = self.client.post("/ingest", content=batch.model_dump_json(), headers={"content-type": "application/json"})
        assert r.status_code == 200, r.text
        self.results.append(r.json())
        return r.json()

    async def collector_time(self):
        return None

    async def close(self) -> None:
        return None


async def test_backfill_through_collector_produces_expected_alarms(plant_path, runtime_path, scenario_path):
    plant = load_plant(plant_path)
    runtime = RuntimeConfig(units=["U-4"], source_id="SIM-U4", time_mode="backfill", backfill_days=2)
    scenario = load_scenarios([scenario_path], plant, runtime)
    with TestClient(create_app(plant_path, runtime_path, ":memory:", scenario_path=scenario_path)) as client:
        sender = ClientSender(client)
        await run_backfill_then_live(plant, runtime, scenario, sender, now=NOW, live_clock=SimClock.backfill(NOW),
                                     live_until=NOW + timedelta(minutes=2))
        assert all(r["rejected"] == 0 and r["duplicate"] is False for r in sender.results)

        st = client.app.state.storage
        assert st.count("ts_raw") == 9 * (2 * 8640 + 12)              # U-4 태그 9개
        assert st.query("SELECT COUNT(*) FROM ts_10m WHERE tag_id = 'VI-M4101'")[0][0] == 2 * 144

        alarms = client.get("/alarms", params={"equip_id": "M-4101"}).json()
        soft = [a for a in alarms if a["tag_id"] == "VI-M4101" and a["limit_type"] == "Soft"]
        assert soft and soft[0]["severity"] == "Low"
        # 1.7 + 3.2×진행률 + 잡음 봉우리 > 2.8 → 진행률 0.6 전후 = 11-06 낮 (경고 상한은 11-04 ~ 11-05 에는 넘지 않는다)
        assert to_db(datetime(2026, 11, 6, 0, 0, tzinfo=KST)) <= soft[0]["occurred_at"] < to_db(datetime(2026, 11, 7, 0, 0, tzinfo=KST))
        assert soft[0]["cleared_at"] is None                            # 아직 진행 중
        assert client.get("/alarms", params={"equip_id": "G-4101"}).json() == []      # 번진 진동(+0.9)은 경고 5.0 아래
        assert client.get("/alarms", params={"equip_id": "P-4101"}).json() == []

        cur = client.get("/tags/VI-M4101/current").json()
        assert cur["status"] == "Good" and cur["value"] > 2.8
        ts = client.get("/timeseries", params={"tag_id": "VI-M4101", "from": (NOW - timedelta(days=2)).isoformat(),
                                               "to": NOW.isoformat()}).json()
        assert len(ts) == 48 and ts[0]["avg"] < ts[-1]["avg"]            # 점진 상승
