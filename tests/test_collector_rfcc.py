"""RFCC 구성안으로 뜬 수집 서버: 한계값 없는 태그, 10초 태그의 NoData 기준, 수신 묶음 정리, 조회 API."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from collector.app import create_app
from common.message import IngestBatch, Sample
from common.timefmt import to_db

T0 = datetime(2026, 11, 9, 1, 0, tzinfo=timezone.utc)


@pytest.fixture
def client(rfcc_plant_path, rfcc_runtime_path, tmp_path):
    app = create_app(rfcc_plant_path, rfcc_runtime_path, tmp_path / "rfcc.db")
    with TestClient(app) as c:
        yield c


def _send(client, tag_id, equip_id, start, count, step_sec, value):
    samples = [Sample(equip_id=equip_id, tag_id=tag_id, ts=start + timedelta(seconds=i * step_sec), value=value, quality="Good")
               for i in range(count)]
    batch = IngestBatch(source_id="T", batch_id=f"b-{tag_id}-{start.timestamp()}", sent_at=start, samples=samples)
    assert client.post("/ingest", content=batch.model_dump_json(), headers={"content-type": "application/json"}).status_code == 200


def test_ten_second_tag_is_good_until_three_of_its_intervals(client):
    _send(client, "TI-P-1101A", "P-1101A", T0, 1, 10, 66.0)
    _send(client, "VI-P-1101A", "P-1101A", T0, 10, 1, 1.45)                 # sim_now = T0+9s
    assert client.get("/tags/TI-P-1101A/current").json()["status"] == "Good"
    _send(client, "VI-P-1101A", "P-1101A", T0 + timedelta(seconds=31), 1, 1, 1.45)
    assert client.get("/tags/TI-P-1101A/current").json()["status"] == "NoData"


def test_no_alarms_without_limits_and_aggregates_exist(client):
    _send(client, "VI-P-1101A", "P-1101A", T0, 600, 1, 9.9)                 # 아무리 커도 한계값이 없다
    _send(client, "VI-P-1101A", "P-1101A", T0 + timedelta(minutes=10), 1, 1, 9.9)
    assert client.get("/alarms").json() == []
    st = client.app.state.storage
    assert st.query("SELECT COUNT(*) FROM ts_10m WHERE tag_id = 'VI-P-1101A' AND count > 0")[0][0] == 1


def test_old_ingest_batches_are_deleted(client):
    st = client.app.state.storage
    st.execute("INSERT INTO ingest_batch VALUES ('b-old', 'T', ?, 0)", (to_db(datetime.now(timezone.utc) - timedelta(days=2)),))
    _send(client, "VI-P-1101A", "P-1101A", T0, 1, 1, 1.45)
    _send(client, "VI-P-1101A", "P-1101A", T0 + timedelta(minutes=10), 1, 1, 1.45)
    assert st.query("SELECT COUNT(*) FROM ingest_batch WHERE batch_id = 'b-old'")[0][0] == 0
    assert st.query("SELECT COUNT(*) FROM ingest_batch")[0][0] == 2


# --- 조회 API (설계 §4.5) ---------------------------------------------------

def test_equipment_listing_follows_hierarchy(client):
    equipment = client.get("/equipment").json()
    assert [e["equip_id"] for e in equipment][:3] == ["P-1101A", "B-1201", "E-1202"]
    assert equipment[0]["unit_group_name"] == "원료 공급" and equipment[1]["driven_by"] == "E-1202"
    tags = {t["tag_id"]: t for e in equipment for t in e["tags"]}
    assert len(tags) == 81 and tags["TI-P-1101A"]["interval_sec"] == 10 and tags["ZI-E-1202"]["decimals"] == 5
    assert tags["TI-P-1101A"]["description"] == "펌프 구동측 베어링 온도"


def test_current_lists_every_tag_with_status(client):
    _send(client, "VI-P-1101A", "P-1101A", T0, 1, 1, 1.45)
    rows = {r["tag_id"]: r for r in client.get("/current").json()}
    assert len(rows) == 81
    assert rows["VI-P-1101A"]["status"] == "Good" and rows["VI-P-1101A"]["value"] == 1.45
    assert rows["TI-P-1101A"]["status"] == "NoData" and rows["TI-P-1101A"]["value"] is None


def test_root_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html") and "<title>" in r.text
