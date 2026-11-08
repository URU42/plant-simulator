"""전송부 (기획서 2장): HTTP POST, 교체 가능. 재시도 뒤 성공을 돌려준다."""
import httpx
import pytest

from common.message import IngestBatch, Sample
from simulator.sender import HttpSender, MemorySender, SendError


def _batch():
    return IngestBatch(source_id="SIM-ALL", batch_id="b-1", sent_at="2026-11-08T17:24:00+09:00",
                       samples=[Sample(equip_id="C-1101", tag_id="VI-C1101", ts="2026-11-08T17:23:10+09:00", value=2.3, quality="Good")])


async def test_http_sender_posts_batch_to_ingest():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["json"] = request.read()
        return httpx.Response(200, json={"batch_id": "b-1", "stored": 1})

    sender = HttpSender("http://collector:8000", transport=httpx.MockTransport(handler))
    result = await sender.send(_batch())
    assert seen["url"] == "http://collector:8000/ingest"
    assert b'"schema_version":"1.0"' in seen["json"]
    assert result["stored"] == 1


async def test_http_sender_retries_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"stored": 1})

    sender = HttpSender("http://c", transport=httpx.MockTransport(handler), retries=3, backoff_sec=0)
    assert (await sender.send(_batch()))["stored"] == 1
    assert calls["n"] == 3


async def test_http_sender_gives_up_after_retries():
    sender = HttpSender("http://c", transport=httpx.MockTransport(lambda r: httpx.Response(500)), retries=2, backoff_sec=0)
    with pytest.raises(SendError):
        await sender.send(_batch())


async def test_memory_sender_keeps_batches():
    sender = MemorySender()
    await sender.send(_batch())
    assert len(sender.batches) == 1
