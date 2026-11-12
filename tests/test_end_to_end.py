"""검증 (기획서 9.2 9단계): 시뮬레이터 → 수집 서버 경로로 이어서 백필하면 빈 구간·거절 없이 저장·집계되고,
수집 서버의 최근 값으로 생성기를 되살린다."""
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from collector.app import create_single_app
from common.config_models import RuntimeConfig, load_plant
from common.message import IngestBatch, Sample
from common.sim_clock import KST, SimClock
from common.timefmt import from_db
from simulator.main import run_backfill_then_live

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=KST)
RESUME = NOW - timedelta(hours=1)            # 수집 서버가 이미 가진 마지막 측정 시각


class ClientSender:
    """TestClient 로 POST /plant/ingest. 이어 보낼 시각과 최근 값은 수집 서버(/health, 저장소)에서 읽는다."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.results = []

    async def send(self, batch: IngestBatch):
        r = self.client.post("/plant/ingest", content=batch.model_dump_json(), headers={"content-type": "application/json"})
        assert r.status_code == 200, r.text
        self.results.append(r.json())
        return r.json()

    async def collector_time(self):
        sim_now = self.client.get("/plant/health").json()["sim_now"]
        return from_db(sim_now) if sim_now else None

    async def recent_values(self, count):
        return self.client.app.state.plants["plant"].storage.recent_values(count)

    async def close(self) -> None:
        return None


async def test_resumed_backfill_through_collector(plant_path, runtime_path):
    plant = load_plant(plant_path)
    runtime = RuntimeConfig(units=["UNIT-RHDS-06"], source_id="SIM-U6", sample_interval_sec=1, send_interval_sec=60,
                            time_mode="backfill", backfill_days=1, raw_retention_days=1)
    keys = [(e.equip_id, t.tag_id) for e in plant.equipment_for_units(runtime.units) for t in e.tags]
    mu = {k: t.control.mean for k, t in plant.tag_by_key().items()}
    with TestClient(create_single_app(plant_path, runtime_path, ":memory:")) as client:
        sender = ClientSender(client)
        seed = [Sample(equip_id=e, tag_id=t, ts=RESUME - timedelta(seconds=10), value=mu[(e, t)], quality="Good") for e, t in keys]
        seed += [Sample(equip_id=e, tag_id=t, ts=RESUME, value=mu[(e, t)], quality="Good") for e, t in keys]
        await sender.send(IngestBatch(source_id="T", batch_id="b-seed", sent_at=RESUME, samples=seed))

        await run_backfill_then_live(plant, runtime, sender, now=NOW, live_clock=SimClock.backfill(NOW),
                                     live_until=NOW + timedelta(minutes=2))
        assert all(r["rejected"] == 0 and r["duplicate"] is False for r in sender.results)

        st = client.app.state.plants["plant"].storage
        per_hour = 4 * 3600 + 360                                     # P-3103: 1초 태그 4개 + 10초 태그 1개
        # 시드 + 이어 보낸 1시간(첫 시각은 시드와 겹쳐 저장되지 않는다) + 실시간 2분
        assert st.count("ts_raw") == 2 * len(keys) + (per_hour - len(keys)) + per_hour // 30
        # 10분 구간: 시드의 10:50 구간 + 이어 보낸 6개 (12:00 구간은 아직 미완료)
        assert st.query("SELECT COUNT(*) FROM ts_10m WHERE tag_id = 'VI-A-P3103' AND count > 0")[0][0] == 7
        assert client.get("/plant/alarms").json() == []               # 한계값 알람은 만들지 않는다

        recent = st.recent_values(3)
        assert set(recent) == set(keys) and all(len(v) == 3 for v in recent.values())
        cur = client.get("/plant/equipment/P-3103/tags/VI-A-P3103/current").json()
        assert cur["status"] in ("Good", "Bad") and cur["value"] == recent[("P-3103", "VI-A-P3103")][-1]
