"""배포 설정 (k8s 설계 §4.2): ROOT_PATH, CORS_ORIGINS, INGEST_TOKEN."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from collector.app import create_single_app
from collector.settings import Settings
from common.message import IngestBatch, Sample

T0 = datetime(2026, 11, 9, 1, 0, tzinfo=timezone.utc)


def _batch():
    s = [Sample(equip_id="C-1101", tag_id="VI-C1101", ts=T0 + timedelta(seconds=10 * i), value=2.0, quality="Good") for i in range(3)]
    return IngestBatch(source_id="T", batch_id="b-tok", sent_at=T0, samples=s).model_dump_json()


def test_ingest_token_blocks_http_but_not_local(plant_path, runtime_path):
    settings = Settings(ingest_token="s3cret")
    with TestClient(create_single_app(plant_path, runtime_path, ":memory:", settings=settings)) as c:
        h = {"content-type": "application/json"}
        assert c.post("/plant/ingest", content=_batch(), headers=h).status_code == 403
        assert c.post("/plant/ingest", content=_batch(), headers=h | {"X-Ingest-Token": "nope"}).status_code == 403
        assert c.post("/plant/ingest", content=_batch(), headers=h | {"X-Ingest-Token": "s3cret"}).json()["stored"] == 3
        ctx = c.app.state.plants["plant"]
        assert ctx.receive_local("b-local", "L", [Sample(equip_id="C-1101", tag_id="VI-C1101", ts=T0 + timedelta(minutes=1), value=2.0, quality="Good")]).stored == 1


def test_root_path_prefixes_links_and_redirect(plant_path, runtime_path):
    with TestClient(create_single_app(plant_path, runtime_path, ":memory:", settings=Settings(root_path="/plant-sim")), root_path="/plant-sim") as c:
        assert "href='/plant-sim/plant/'" in c.get("/").text
        assert c.get("/plant", follow_redirects=False).headers["location"] == "/plant-sim/plant/"
        assert c.get("/plant/health").status_code == 200


def test_cors_header_when_origins_set(plant_path, runtime_path):
    with TestClient(create_single_app(plant_path, runtime_path, ":memory:", settings=Settings(cors_origins=["https://u4-2.skala-us.com"]))) as c:
        r = c.get("/plant/health", headers={"Origin": "https://u4-2.skala-us.com"})
        assert r.headers.get("access-control-allow-origin") == "https://u4-2.skala-us.com"


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("ROOT_PATH", "/x"); monkeypatch.setenv("CORS_ORIGINS", "a,b"); monkeypatch.setenv("WITH_SIMULATORS", "1")
    monkeypatch.delenv("INGEST_TOKEN", raising=False)
    s = Settings.from_env()
    assert (s.root_path, s.cors_origins, s.with_simulators, s.ingest_token) == ("/x", ["a", "b"], True, None)
