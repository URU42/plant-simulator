"""Nelson 알람 이벤트의 Redis Streams 발행: 열림·닫힘만, 실시간 구간에서만, Redis 오류는 수신을 막지 않는다."""
import pytest
import redis
from fastapi.testclient import TestClient

from collector.alarm_stream import MAXLEN, STREAM, AlarmStream
from collector.app import create_single_app
from collector.settings import Settings
from common.nelson import QUIET
from tests.test_collector_alarms import post, rows


@pytest.fixture
def rhds_client(plant_path, runtime_path):
    with TestClient(create_single_app(plant_path, runtime_path, ":memory:", key="plant")) as c:
        yield c


class FakeStream:
    """AlarmStream 대신 받은 (공장, 이벤트) 를 모은다."""

    def __init__(self):
        self.sent = []

    def publish(self, plant, events):
        self.sent += [(plant, e) for e in events]


def test_opened_and_cleared_events_match_alarm_rows(rhds_client):
    ctx = rhds_client.app.state.plants["plant"]
    ctx.alarm_stream, ctx.live = FakeStream(), True
    post(rhds_client, list(QUIET) * 4 + [3.5, 3.5, 3.5] + list(QUIET) * 3)      # 같은 묶음에서 열리고 닫힌다
    post(rhds_client, [3.5] * 3, start=100)                                     # 열리고 아직 열림
    post(rhds_client, list(QUIET) * 2, start=103)                               # 위 알람이 닫힌다
    alarm = {a["alarm_id"]: a for a in rows(rhds_client) if a["rule"] == 1}
    sent = [(e["event"], e["alarm_id"]) for p, e in ctx.alarm_stream.sent if e["rule"] == 1]
    first, second = sorted(alarm)
    assert sent == [("opened", first), ("cleared", first), ("opened", second), ("cleared", second)]
    assert all(p == "plant" for p, _ in ctx.alarm_stream.sent)
    cleared = next(e for _, e in ctx.alarm_stream.sent if e["event"] == "cleared" and e["alarm_id"] == second)
    assert {k: v for k, v in cleared.items() if k != "event"} == alarm[second]   # 닫힘 이벤트는 그 행 전체


def test_nothing_published_before_live(rhds_client):
    ctx = rhds_client.app.state.plants["plant"]
    ctx.alarm_stream = FakeStream()
    post(rhds_client, list(QUIET) * 4 + [3.5, 3.5, 3.5])
    assert rows(rhds_client) and ctx.alarm_stream.sent == []


class FakePipeline:
    def __init__(self, fail):
        self.calls, self.fail = [], fail

    def xadd(self, name, fields, maxlen=None, approximate=False):
        self.calls.append((name, fields, maxlen, approximate))

    def execute(self):
        if self.fail:
            raise redis.ConnectionError("down")


class FakeRedis:
    def __init__(self, fail=False):
        self.pipe = FakePipeline(fail)

    def pipeline(self, transaction=True):
        return self.pipe


def test_publish_xadds_strings_with_approximate_maxlen():
    client = FakeRedis()
    AlarmStream(client).publish("rhds", [{"event": "opened", "alarm_id": 7, "direction": None, "value_at_occurrence": 1.5}])
    assert client.pipe.calls == [(STREAM, {"plant": "rhds", "event": "opened", "alarm_id": "7", "direction": "",
                                           "value_at_occurrence": "1.5"}, MAXLEN, True)]


def test_publish_swallows_redis_errors(caplog):
    AlarmStream(FakeRedis(fail=True)).publish("rhds", [{"event": "opened", "alarm_id": 1}])
    assert "alarm stream" in caplog.text


def test_redis_url_from_env(monkeypatch, plant_path, runtime_path):
    monkeypatch.setenv("REDIS_URL", "redis://clx-redis:6379")
    settings = Settings.from_env()
    assert settings.redis_url == "redis://clx-redis:6379"
    with TestClient(create_single_app(plant_path, runtime_path, ":memory:", settings=settings)) as c:
        assert isinstance(c.app.state.plants["plant"].alarm_stream, AlarmStream)   # 연결은 첫 발행 때 (지금 Redis 가 없어도 뜬다)
    assert Settings().redis_url is None
