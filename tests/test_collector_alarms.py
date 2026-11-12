"""수집 서버 상시 Nelson 판정과 /alarms (설계 2026-11-12-nelson-anomaly §4.8)."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from collector.app import create_single_app
from common.message import IngestBatch, Sample
from common.nelson import QUIET

MU, SIG, T0 = 33.1, 0.8249781141878183, datetime(2026, 11, 12, tzinfo=timezone.utc)   # RHDS C-3101/XI-X-C3101


@pytest.fixture
def rhds_db_path(tmp_path):
    return tmp_path / "alarms.db"


@pytest.fixture
def rhds_app_factory(plant_path, runtime_path):
    return lambda db: create_single_app(plant_path, runtime_path, db, key="plant")


@pytest.fixture
def rhds_client(rhds_db_path, rhds_app_factory):
    with TestClient(rhds_app_factory(rhds_db_path)) as c:
        yield c


def post(client, zs, start=0, status="Good"):
    samples = [Sample(equip_id="C-3101", tag_id="XI-X-C3101", ts=T0 + timedelta(seconds=start + i),
                      value=round(MU + SIG * z, 4), status=status) for i, z in enumerate(zs)]
    batch = IngestBatch(source_id="T", batch_id=f"b{start}", sent_at=T0, samples=samples)
    assert client.post("/plant/ingest", content=batch.model_dump_json(), headers={"content-type": "application/json"}).status_code == 200


def rows(client, **q):
    return client.get("/plant/alarms", params=q).json()


def test_rule1_opens_and_closes(rhds_client):
    post(rhds_client, list(QUIET) * 4 + [3.5, 3.5, 3.5] + list(QUIET) * 3)
    r = [a for a in rows(rhds_client) if a["rule"] == 1]
    assert len(r) == 1 and r[0]["hit_count"] == 3 and r[0]["direction"] == "hi"
    assert r[0]["occurred_at"].startswith("2026-11-12T00:00:32") and r[0]["cleared_at"].startswith("2026-11-12T00:00:35")
    assert set(r[0]) == {"alarm_id", "equip_id", "tag_id", "rule", "direction", "occurred_at", "cleared_at",
                         "last_hit_at", "hit_count", "value_at_occurrence"}


def test_alarms_overlap_window(rhds_client):
    post(rhds_client, list(QUIET) * 4 + [3.5] + list(QUIET) * 3)          # 00:00:32 열림, 00:00:33 닫힘
    post(rhds_client, [3.5] * 3, start=100)                               # 00:01:40 열림, 아직 열림
    in_window = rows(rhds_client, **{"from": "2026-11-12T00:01:50Z", "to": "2026-11-12T00:05:00Z"})
    assert [a["occurred_at"][:19] for a in in_window if a["rule"] == 1] == ["2026-11-12T00:01:40"]


def test_rule_filter_validated(rhds_client):
    assert rhds_client.get("/plant/alarms", params={"rule": 9}).status_code == 422


def test_open_alarm_survives_restart(rhds_db_path, rhds_app_factory):
    with TestClient(rhds_app_factory(rhds_db_path)) as c:
        post(c, list(QUIET) * 4 + [3.5, 3.5])
    with TestClient(rhds_app_factory(rhds_db_path)) as c:
        post(c, [3.5], start=34)
        r = [a for a in rows(c) if a["rule"] == 1]
        assert len(r) == 1 and r[0]["hit_count"] == 3 and r[0]["cleared_at"] is None


def test_status_mismatch_is_logged_but_alarm_recorded(rhds_client, caplog):
    post(rhds_client, list(QUIET) * 4 + [3.5], status="Good")
    assert any(a["rule"] == 1 for a in rows(rhds_client))
    assert "status mismatch" in caplog.text


def test_duplicate_batch_not_counted_twice(rhds_client):
    post(rhds_client, list(QUIET) * 4 + [3.5, 3.5])
    post(rhds_client, list(QUIET) * 4 + [3.5, 3.5])
    assert [a["hit_count"] for a in rows(rhds_client) if a["rule"] == 1] == [2]


def test_miss_streak_carries_across_batches(rhds_client):
    post(rhds_client, list(QUIET) * 4 + [3.5])                            # 00:00:32 열림
    post(rhds_client, list(QUIET), start=33)                              # 미성립 8개: 아직 열림
    assert [a["rule"] for a in rows(rhds_client, open="true")].count(1) == 1
    post(rhds_client, list(QUIET), start=41)                              # 미성립 16개: 첫 미성립 시각으로 닫힘
    assert [a["rule"] for a in rows(rhds_client, open="true")].count(1) == 0
    assert [a["cleared_at"][:19] for a in rows(rhds_client, rule=1)] == ["2026-11-12T00:00:33"]


def test_reopens_after_close_in_one_batch(rhds_client):
    post(rhds_client, list(QUIET) * 4 + [-3.5] + list(QUIET) * 2 + [-3.5] + list(QUIET) * 2)   # 32·49 에서 성립
    r = rows(rhds_client, rule=1)
    assert [(a["occurred_at"][:19], a["cleared_at"][:19], a["direction"]) for a in r] == [
        ("2026-11-12T00:00:49", "2026-11-12T00:00:50", "lo"), ("2026-11-12T00:00:32", "2026-11-12T00:00:33", "lo")]
    assert r[1]["value_at_occurrence"] == round(MU - SIG * 3.5, 4)


def rule3_alarm_rows(config_dir, tmp_path) -> dict[tuple[str, int], int]:
    """RHDS P-3101 에 12분짜리 규칙 3 사건 하나를 만들어 수집 서버 판정에 넣고, (태그, 규칙)별 nelson_alarm 행 수를 센다."""
    from collector.alarms import AlarmTracker
    from collector.seed_master import seed_master
    from collector.storage import Storage
    from common.config_models import load_plant, load_runtime
    from common.timefmt import to_db
    from simulator.anomaly import AnomalyEvent
    from simulator.nelson_model import NelsonGenerator, NoEvents

    class One(NoEvents):
        def external(self, start, end):
            return [event]

    plant, rt = load_plant(config_dir / "plant_rhds.yaml"), load_runtime(config_dir / "runtime_rhds.yaml")
    rt = rt.model_copy(update={"units": [plant.equipment_by_id()["P-3101"].unit_id]})
    t = T0.timestamp() + 60
    event = AnomalyEvent("I1", "injected", "P-3101", 3, t, t + 720)
    samples = NelsonGenerator(plant, rt, One(), spontaneous=False).generate(T0, T0 + timedelta(minutes=15))
    st = Storage(tmp_path / "r3.db"); seed_master(st, plant)
    AlarmTracker(st, plant).observe([(s.equip_id, s.tag_id, to_db(s.ts), s.value, s.status) for s in samples])
    return {(r[0], r[1]): r[2] for r in st.query(
        "SELECT tag_id, rule, COUNT(*) FROM nelson_alarm WHERE equip_id = 'P-3101' GROUP BY tag_id, rule")}


def test_rule3_sawtooth_event_is_few_alarm_rows(config_dir, tmp_path):
    """톱니(주기 16)의 양 끝에서 나는 규칙 5·8 위반 사이가 닫힘(15 표본)보다 짧아, 12분 사건이 태그·규칙마다 몇 행으로 묶인다."""
    counts = rule3_alarm_rows(config_dir, tmp_path)
    assert counts.get(("VI-A-P3101", 3), 0) >= 1                          # 대표 태그에 규칙 3 이 난다
    assert all(n <= 2 for (_, rule), n in counts.items() if rule in (5, 8)), counts
