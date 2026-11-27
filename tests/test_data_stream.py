"""측정값 Redis Stream: 초마다 메시지 하나 {plant, plant_id, ts, samples}, plant-sim:data:{공장}, MAXLEN ~ 30분."""
import json
from datetime import datetime, timedelta, timezone

import redis

from collector.data_stream import MAXLEN, DataStream
from common.message import Sample


class FakeClient:
    def __init__(self):
        self.added = []

    def pipeline(self, transaction=True):
        return self

    def xadd(self, name, fields, maxlen=None, approximate=False):
        self.added.append((name, json.loads(fields["data"]), maxlen, approximate))

    def execute(self):
        pass

    def close(self):
        pass


def test_messages_per_second_with_ten_second_tags_only_on_their_second(rfcc_plant_path, rfcc_runtime_path):
    """receive 한 묶음을 초별 메시지로 나눠 보낸다. 10초 태그(TI)는 0초 메시지에만 있다."""
    from collector.plant_context import PlantContext
    from collector.plants import PlantEntry
    ctx = PlantContext.open(PlantEntry(key="rfcc", plant=str(rfcc_plant_path), runtime=str(rfcc_runtime_path), db=":memory:"), modes={})
    fake = FakeClient()
    ctx.data_stream = DataStream(fake, "rfcc")
    t0 = datetime.now(timezone.utc).replace(microsecond=0)
    t0 -= timedelta(seconds=t0.second % 10)
    samples = [Sample(equip_id="P-1101A", tag_id="VI-P-1101A", ts=t0 + timedelta(seconds=i), value=1.4) for i in range(3)]
    samples.append(Sample(equip_id="P-1101A", tag_id="TI-P-1101A", ts=t0, value=66.0))
    ctx.receive("b1", "T", samples, t0.isoformat())
    ctx.data_stream.close()
    ctx.close()
    assert [a[0] for a in fake.added] == ["plant-sim:data:rfcc"] * 3
    msgs = [a[1] for a in fake.added]
    assert all(set(m) == {"plant", "plant_id", "ts", "samples"} and m["plant"] == "rfcc" and m["plant_id"] == "RFCC" for m in msgs)
    assert [len(m["samples"]) for m in msgs] == [2, 1, 1] and [m["ts"] for m in msgs] == sorted(m["ts"] for m in msgs)
    assert set(msgs[0]["samples"][0]) == {"equip_id", "tag_id", "ts", "value"}
    assert all(a[2] == MAXLEN == 1800 and a[3] for a in fake.added)       # 초당 1건 × 30분, 대략 자르기


def test_old_messages_are_not_sent():
    """실제 시각 기준 30분보다 오래된 값(백필의 지난 값)은 보내지 않는다."""
    fake = FakeClient()
    s = DataStream(fake, "rhds")
    old = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
    new = datetime.now(timezone.utc).isoformat()
    s.submit([{"plant": "rhds", "ts": old, "samples": []}, {"plant": "rhds", "ts": new, "samples": []}])
    s.close()
    assert [a[1]["ts"] for a in fake.added] == [new]


def test_redis_down_does_not_raise():
    """Redis 가 없어도 수신 경로는 멈추지 않는다 (경고만 남기고 버린다)."""
    s = DataStream(redis.Redis.from_url("redis://127.0.0.1:1", socket_timeout=0.2, socket_connect_timeout=0.2), "cdu")
    s.send([{"plant": "cdu", "ts": datetime.now(timezone.utc).isoformat(), "samples": []}])
    s.close()


def test_controlled_simulation_never_publishes(config_dir):
    """조작 시뮬레이션(일부러 낸 고장)은 REDIS_URL 이 있어도 AE 입력 Stream 으로 나가지 않는다. 그 밖의 공장은 그대로 보낸다."""
    from collector.plant_context import PlantContext
    from collector.plants import PlantEntry
    for runtime, control, sends in (("runtime_rhds_twin.yaml", True, False), ("runtime_rhds_twin.yaml", False, True),
                                    ("runtime_rhds.yaml", True, True)):
        ctx = PlantContext.open(PlantEntry(key="rhds", plant=str(config_dir / "plant_rhds.yaml"), runtime=str(config_dir / runtime),
                                           db=":memory:"), {}, "redis://127.0.0.1:1", simulation_control=control)
        assert (ctx.data_stream is not None) == sends, (runtime, control)
        ctx.close()
