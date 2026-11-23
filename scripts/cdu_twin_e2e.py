"""인증된 실제 Backend에서 CDU 정상→막힘→후속 변화→mock 진단을 재현한다."""
import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from statistics import mean
from uuid import uuid4

import httpx

ROOT = "/api/v1/twin/cdu"
FEED = ("P-8101A", "PI-8101A")
PRODUCT = ("P-8401A", "FI-8401A")
SUPPLY = ("P-8101A", "FI-8101A")
CACHE = Path(__file__).resolve().parents[1] / ".cache" / "d4"


async def run(base: str, username: str, password: str) -> dict:
    async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=12) as client:
        async def request(method, path, body=None, expected=200, headers=None):
            response = await client.request(method, path, json=body, headers=headers)
            if response.status_code != expected:
                raise AssertionError(f"{method} {path}: HTTP {response.status_code}, expected {expected}")
            return response.json()

        await request("GET", ROOT + "/state", expected=401)
        csrf = await request("GET", "/api/v1/auth/csrf")
        grant = await request("POST", "/api/v1/auth/login", {"username": username, "password": password},
                              headers={csrf["header_name"]: csrf["token"]})
        client.headers["Authorization"] = "Bearer " + grant["access_token"]
        checks = []

        async def command(action, *, command_id=None, **fields):
            return await request("POST", ROOT + "/control", {"command_id": command_id or uuid4().hex,
                                                               "action": action, **fields})

        initial = await request("GET", ROOT + "/state")
        assert initial["simulation"]["phase"] == "ready", "새 로컬 시연 인스턴스에서 실행하세요."
        assert any(mode["mode"] == "PM-SUCTION-LOSS" and FEED[0] in mode["equip_ids"] for mode in initial["modes"])
        await command("speed", rate=30)
        assert (await command("start"))["phase"] == "running"
        checks.append("무인증 차단, 시나리오 목록, 준비→시작, 30배속")

        pending = asyncio.Queue(maxsize=256)
        async def stream():
            async with client.stream("GET", ROOT + "/stream", timeout=None) as response:
                response.raise_for_status()
                event = None
                async for line in response.aiter_lines():
                    if line.startswith("event: "):
                        event = line[7:]
                    if line.startswith("data: ") and event == "samples":
                        data = json.loads(line[6:])
                        for sample in data:
                            assert set(sample) == {"equip_id", "tag_id", "ts", "value"}
                        if pending.full():
                            pending.get_nowait()
                        pending.put_nowait(data)

        last = {}
        async def observations(seconds, predicate=None):
            values = []
            async with asyncio.timeout(45):
                while len(values) < seconds or (predicate and not predicate(values)):
                    batch = await pending.get()
                    by_key = {(s["equip_id"], s["tag_id"]): s for s in batch}
                    if not all(key in by_key for key in (FEED, SUPPLY, PRODUCT)):
                        continue
                    now = datetime.fromisoformat(by_key[FEED]["ts"])
                    if "time" in last and now <= last["time"]:
                        continue
                    last["time"] = now
                    values.append({"ts": now.isoformat(), "suction": by_key[FEED]["value"],
                                   "feed": by_key[SUPPLY]["value"], "product": by_key[PRODUCT]["value"]})
            return values[-seconds:]

        task = asyncio.create_task(stream())
        try:
            normal = await observations(12)
            assert mean(s["suction"] for s in normal) > 2.5
            normal_product = mean(s["product"] for s in normal)
            paused = await command("pause")
            await asyncio.sleep(0.35)
            assert (await request("GET", ROOT + "/state"))["simulation"] == paused
            assert (await command("resume"))["phase"] == "running"
            checks.append("정상 센서 SSE, 일시정지 중 시계 동결, 재개")

            # 연결을 끊고 새 연결에서 현재값과 타임스탬프가 다시 합쳐지는지 확인한다.
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            snapshot = await request("GET", ROOT + "/state")
            assert any((row["equip_id"], row["tag_id"]) == FEED and row["ts"] for row in snapshot["current"])
            task = asyncio.create_task(stream())
            checks.append("SSE 재연결 후 현재값 재동기화")

            command_id = uuid4().hex
            fields = {"mode": "PM-SUCTION-LOSS", "equip_id": FEED[0], "scenario_speed": 10}
            first = await command("inject", command_id=command_id, **fields)
            assert await command("inject", command_id=command_id, **fields) == first
            await request("POST", ROOT + "/control", {"command_id": command_id, "action": "pause"}, expected=409)
            checks.append("막힘 주입 재시도 중복 제거 및 명령 ID 충돌 거부")

            affected = await observations(12, lambda rows:
                rows[-1]["suction"] < 1.90
                and mean(s["product"] for s in rows[-12:]) < normal_product - 0.6)
            assert mean(s["feed"] for s in affected) < mean(s["feed"] for s in normal) - 8
            diagnosis = await request("POST", ROOT + "/diagnosis")
            assert diagnosis["mode"] == "mock" and diagnosis["status"] == "needs_review"
            assert diagnosis["equipment_id"] == FEED[0]
            assert any(s["id"] == FEED[1] for s in diagnosis["sources"])
            assert "fault_id" not in json.dumps(diagnosis) and fields["mode"] not in json.dumps(diagnosis)
            checks.append("흡입압 경보·주 유량 감소·후속 유량 감소 및 관측 기반 mock 진단")

            await command("normal")
            recovered = await observations(5, lambda rows: rows[-1]["suction"] > 2.65)
            assert recovered[-1]["suction"] > affected[-1]["suction"]
            await command("pause")
            checks.append("정상 운전 전환 후 회복")
            return {"mode": "mock", "checks": checks, "normal": normal, "affected": affected,
                    "diagnosis": diagnosis, "recovered": recovered}
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:5189")
    parser.add_argument("--output", type=Path, required=True, help="Git에서 제외된 .cache/d4 아래 새 JSON 파일")
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to(CACHE):
        parser.error("합성 데이터 출력은 .cache/d4 내부여야 합니다.")
    user, password = os.environ.get("D4_USERNAME"), os.environ.get("D4_PASSWORD")
    if not user or not password:
        parser.error("D4_USERNAME, D4_PASSWORD를 설정하세요.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        report = asyncio.run(run(args.base, user, password))
        json.dump(report, output, indent=2, ensure_ascii=False, allow_nan=False)
        output.write("\n")
    for check in report["checks"]:
        print("PASS", check)
    print("계측·진단 결과", args.output)


if __name__ == "__main__":
    main()
