import asyncio
import csv
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from collector.anomaly_book import AnomalyBook, NotApplicable
from collector.app import create_single_app
from collector.seed_master import seed_master
from collector.settings import Settings
from collector.storage import Storage
from common.config_models import load_plant, load_runtime
from common.message import IngestBatch, Sample
from common.sim_clock import KST
from simulator.fault import load_fault_modes
from simulator.instrument import InstrumentEvent
from simulator.model import PlantGenerator
from simulator.physics_adapter import PhysicsAdapter
from simulator.physics_scenario import write_example

START = datetime(2026, 11, 24, tzinfo=KST)
EPOCH = int(START.timestamp())
FLOW = ("P-8101A", "FI-8101A")
PRODUCT = ("P-8401A", "FI-8401A")
SUCTION = ("P-8101A", "PI-8101A")
HEADER = ("P-8101A", "PI-8101B")


@pytest.fixture
def coupled(config_dir):
    plant = load_plant(config_dir / "plant_cdu.yaml")
    runtime = load_runtime(config_dir / "runtime_cdu.yaml").model_copy(update={
        "physics_general": False, "physics": config_dir / "physics_cdu_integrated.yaml", "units": ["UNIT-CDU-01", "UNIT-CDU-07"],
    })
    modes = load_fault_modes(config_dir / "fault_modes.yaml")
    storage = Storage(":memory:")
    seed_master(storage, plant)
    physics = PhysicsAdapter(plant, runtime.physics)
    try:
        yield plant, runtime, physics, AnomalyBook(storage, plant, modes, physics=physics)
    finally:
        storage.close()


def _samples(generator, start=0, end=151):
    return generator.generate(START + timedelta(seconds=start), START + timedelta(seconds=end))


def _values(samples, second):
    return {(s.equip_id, s.tag_id): s.value for s in samples if s.ts.timestamp() == EPOCH + second}


def test_clog_normal_recovery_and_noise_without_double_fault(coupled):
    plant, runtime, physics, book = coupled
    event = book.inject("P-8101A", "PM-SUCTION-LOSS", EPOCH + 10, 10)
    book.stop("P-8101A", EPOCH + 80)
    baseline = _samples(PlantGenerator(plant, runtime))
    generator = PlantGenerator(plant, runtime, book)
    samples = _samples(generator)
    assert samples == _samples(PlantGenerator(plant, runtime, book))
    split = PlantGenerator(plant, runtime, book)
    assert samples == _samples(split, 0, 59) + _samples(split, 59, 151)
    plans = physics.parameter_plans(book.external(EPOCH, EPOCH + 151))

    for second in (5, 55, 75, 105, 145):
        state = physics.state(EPOCH + second, plans)
        centers = physics.sensor_centers(state)
        reference = physics.sensor_centers(physics.solve(0, 0, 0))
        assert abs(state.mass_residual_kg_s) < 1e-9
        assert all(abs(b.pressure_residual_pa) < 1e-4 for b in state.branches)
        observed, nominal = _values(samples, second), _values(baseline, second)
        for key in (FLOW, SUCTION, HEADER, PRODUCT):
            assert observed[key] - nominal[key] == pytest.approx(centers[key] - reference[key], abs=0.00011)
        if second in (5, 145):
            assert centers == reference
        else:
            assert all(centers[k] < reference[k] for k in (FLOW, SUCTION, HEADER, PRODUCT))
    assert all(s.ts.second % 10 == 0 for s in samples if s.tag_id.startswith("TI-"))
    assert sum(s.tag_id == "TI-8101A" for s in samples) == 16
    assert sum(s.tag_id == "FI-8101A" for s in samples) == 151
    assert _values(samples, 70)[("P-8101A", "VI-8101A")] > _values(baseline, 70)[("P-8101A", "VI-8101A")]
    assert all(set(s.model_dump()) == {"equip_id", "tag_id", "ts", "value"} for s in samples)
    truth = book.answer_key(None, None, None)[0]
    assert truth["event_id"] == event.event_id and truth["expect"] is None
    assert truth["physical_parameters"] == [
        {"stage": "S1", "parameter": "strainer_blockage", "amount": 0.45, "rise_sec": 600},
    ]
    assert {(t["equip_id"], t["tag_id"]) for t in truth["response_tags"] if t["via"] == "physics"} == physics.tag_keys


def test_wear_holds_until_repair_and_valve_redistributes(coupled):
    plant, runtime, physics, book = coupled
    original = physics.solve(0, 0, 0)
    wear = book.inject("P-8101A", "PM-IMPELLER-WEAR", EPOCH + 10, 10)
    book.stop("P-8101A", EPOCH + 50)
    assert [e.event_id for e in book.damaged()] == [wear.event_id]
    assert book.external(EPOCH + 90, EPOCH + 99)
    assert book.repair("P-8101A", EPOCH + 100)[1] == [wear.event_id]
    plans = physics.parameter_plans(book.external(EPOCH, EPOCH + 170))
    worn = physics.state(EPOCH + 60, plans)
    assert worn == physics.state(EPOCH + 99, plans)
    assert worn.pump_head_m < original.pump_head_m
    assert worn.hydraulic_w / worn.shaft_w < original.hydraulic_w / original.shaft_w
    assert physics.state(EPOCH + 130, plans).pump_flow_m3_s > worn.pump_flow_m3_s
    assert physics.state(EPOCH + 160, plans) == original
    sensor = _samples(PlantGenerator(plant, runtime, book), end=161)
    assert _values(sensor, 80)[FLOW] < _values(sensor, 160)[FLOW] - 10

    event = book.inject("P-8101A", "PM-DISCHARGE-VALVE-RESTRICTION", EPOCH + 180, 10)
    plans = physics.parameter_plans([event])
    valve = physics.state(EPOCH + 210, plans)
    assert valve.branches[1].flow_m3_s < original.branches[1].flow_m3_s
    assert valve.branches[0].flow_m3_s > original.branches[0].flow_m3_s
    assert valve.discharge_abs_pa > original.discharge_abs_pa
    assert valve.suction_abs_pa == original.suction_abs_pa


def test_existing_instrument_dropout_only_changes_observations(coupled):
    plant, runtime, physics, book = coupled
    source = PlantGenerator(plant, runtime, book)
    baseline = _samples(source, end=5)
    generator = PlantGenerator(plant, runtime, book)
    generator.instrument = type("FixedSchedule", (), {"events": lambda self, start, end: [
        InstrumentEvent("evaluation-only", "dropout", FLOW[0], (FLOW[1],), EPOCH + 1, EPOCH + 3),
    ]})()
    measured = _samples(generator, end=5)
    assert measured == [s for s in baseline if (s.equip_id, s.tag_id) != FLOW or s.ts.second not in (1, 2)]
    assert physics.state(EPOCH + 2, []) == physics.solve(0, 0, 0)


def test_opt_in_api_preserves_measurements_and_gates_scenario_truth(coupled, config_dir, tmp_path):
    plant, runtime, _, _ = coupled
    path = tmp_path / "runtime.yaml"
    path.write_text(runtime.model_dump_json(), encoding="utf-8")
    app = create_single_app(config_dir / "plant_cdu.yaml", path, ":memory:",
                            settings=Settings(with_simulators=False, ingest_token="operator", eval_token="evaluation"))
    token = {"X-Ingest-Token": "operator"}
    with TestClient(app) as client:
        ctx = app.state.plants["plant"]
        assert {m["mode"] for m in client.get("/plant/fault-modes").json()} >= {
            "PM-IMPELLER-WEAR", "PM-DISCHARGE-VALVE-RESTRICTION",
        }
        now = datetime.now(KST).replace(microsecond=0)
        ctx.book.inject(FLOW[0], "PM-SUCTION-LOSS", now.timestamp() - 1)
        samples = PlantGenerator(plant, runtime, ctx.book).generate(now, now + timedelta(seconds=1))
        batch = IngestBatch(source_id=runtime.source_id, batch_id="physics-test", sent_at=now, samples=samples)
        stream = ctx.broadcaster.subscribe()
        future = client.portal.start_task_soon(anext, stream)

        async def subscribed():
            async with asyncio.timeout(2):
                while not ctx.broadcaster.has_subscribers():
                    await asyncio.sleep(0)

        client.portal.call(subscribed)
        result = client.post("/plant/ingest", json=batch.model_dump(mode="json"), headers=token)
        event = future.result(timeout=2)
        client.portal.call(stream.aclose)
        name, payload = event.splitlines()[:2]
        assert name == "event: samples"
        assert json.loads(payload.removeprefix("data: ")) == [s.model_dump(mode="json") for s in samples]
        assert result.status_code == 200 and result.json()["stored"] == len(samples)
        row = client.get(f"/plant/equipment/{FLOW[0]}/tags/{FLOW[1]}/current").json()
        assert row["value"] == next(s.value for s in samples if (s.equip_id, s.tag_id) == FLOW)
        assert "PM-SUCTION-LOSS" not in str(row) and "event_id" not in row
        assert client.get("/plant/anomalies", headers=token).status_code == 403
        assert client.get("/plant/anomalies", headers={"X-Eval-Token": "evaluation"}).status_code == 200
        for path in ("anomalies/active", "anomalies/damaged"):
            assert client.get(f"/plant/{path}").status_code == 403
            assert client.get(f"/plant/{path}", headers=token).status_code == 200

    disabled = AnomalyBook(Storage(":memory:"), plant, load_fault_modes(config_dir / "fault_modes.yaml"))
    try:
        with pytest.raises(NotApplicable):
            disabled.inject(FLOW[0], "PM-IMPELLER-WEAR", EPOCH)
    finally:
        disabled.storage.close()


def test_example_exports_observations_and_evaluation_separately(config_dir, tmp_path):
    output = tmp_path / "example"
    write_example(output, config_dir)
    with (output / "observations" / "measurements.csv").open(encoding="utf-8") as file:
        samples = [Sample.model_validate(row) for row in csv.DictReader(file)]
    truth = json.loads((output / "evaluation" / "truth.json").read_text(encoding="utf-8"))
    assert {event["mode"] for event in truth["events"]} == {"PM-SUCTION-LOSS", "PM-IMPELLER-WEAR"}
    assert len({s.ts for s in samples}) == len(truth["states"])
    assert truth["states"][0]["pump_flow_m3_s"] == truth["states"][-1]["pump_flow_m3_s"]


@pytest.mark.parametrize(('mode', 'amount', 'fault', 'directions'), [
    ('PM-SUCTION-LOSS', 1.0, True, (-1, -1, -1, -1, -1, -1, -1)),
    ('PM-IMPELLER-WEAR', 1.0, True, (0, -1, -1, 1, -1, -1, -1)),
    ('PM-DISCHARGE-VALVE-RESTRICTION', 1.0, True, (0, 1, -1, -1, -1, -1, -1)),
    ('OP-THROUGHPUT-CHANGE', 0.2, False, (0, 1, -1, -1, -1, -1, -1)),
])
def test_all_cdu_operating_scenarios(coupled, mode, amount, fault, directions):
    plant, runtime, physics, book = coupled
    event = book.inject(physics.primary, mode, EPOCH + 10, 10)
    assert (event.mode.label.code != 1) == fault
    book.stop(physics.primary, EPOCH + 180)
    wear = mode == 'PM-IMPELLER-WEAR'
    if wear:
        assert book.repair(physics.primary, EPOCH + 250)[1] == [event.event_id]
    plans = physics.parameter_plans(book.external(EPOCH, EPOCH + 320))
    normal = physics.solve(0, 0, 0)
    shifted = physics.state(EPOCH + 165, plans)
    assert shifted == physics.solve(amount if mode == 'PM-SUCTION-LOSS' else 0, amount if wear else 0,
                                    amount if mode in {'OP-THROUGHPUT-CHANGE', 'PM-DISCHARGE-VALVE-RESTRICTION'} else 0)
    assert abs(shifted.mass_residual_kg_s) < 1e-9
    assert abs(shifted.pump_pressure_residual_pa) < 1e-4
    assert physics.state(EPOCH + 20, plans) not in (normal, shifted)
    assert (physics.state(EPOCH + 245, plans) == shifted) == wear
    assert physics.state(EPOCH + 315, plans) == normal
    tags = (SUCTION, HEADER, FLOW, ('P-8101A', 'II-8101A'), PRODUCT, ('P-8401A', 'PI-8401B'), ('P-8401A', 'II-8401A'))
    nominal, generated = (_samples(PlantGenerator(plant, runtime, source), end=320) for source in (None, book))
    samples = _values(generated, 165)
    baseline = _values(nominal, 165)
    expected = physics.sensor_centers(shifted)
    reference = physics.sensor_centers(normal)
    for tag, direction in zip(tags, directions, strict=True):
        delta = expected[tag] - reference[tag]
        assert delta == 0 if direction == 0 else delta * direction > 0
        assert samples[tag] - baseline[tag] == pytest.approx(delta, abs=0.00011)
        assert _values(generated, 315)[tag] == _values(nominal, 315)[tag]


def test_simulation_observations_returns_only_bounded_observations(coupled, config_dir, tmp_path):
    plant, runtime, _, _ = coupled
    path = tmp_path / 'runtime.yaml'
    path.write_text(runtime.model_dump_json(), encoding='utf-8')
    settings = Settings(with_simulators=False, ingest_token='local-test')
    app = create_single_app(config_dir / 'plant_cdu.yaml', path, ':memory:', settings=settings)
    from common.sim_clock import ControlledClock
    with TestClient(app) as client:
        ctx = app.state.plants['plant']
        ctx.simulation_clock = ControlledClock(START + timedelta(seconds=10))
        samples = _samples(PlantGenerator(plant, runtime), start=8, end=13)
        ctx.receive_local('observations-test', runtime.source_id, samples)
        route = '/plant/simulation/observations'
        params = {'equip_id': PRODUCT[0], 'from': (START + timedelta(seconds=9)).isoformat(),
                  'to': (START + timedelta(seconds=12)).isoformat()}
        assert client.get(route, params=params).status_code in (401, 403)
        headers = {'X-Ingest-Token': 'local-test'}
        response = client.get(route, params=params, headers=headers)
        assert response.status_code == 200
        rows = response.json()
        assert rows and {row['equip_id'] for row in rows} == {PRODUCT[0]}
        assert all(set(row) == {'equip_id', 'tag_id', 'ts', 'value', 'received_at'} for row in rows)
        from datetime import timezone
        assert {datetime.fromisoformat(row['ts']) for row in rows} == {
            (START + timedelta(seconds=second)).astimezone(timezone.utc) for second in (9, 10)
        }
        assert sum(len(asset['tags']) for asset in client.get('/plant/equipment').json()) == 59


def test_live_dynamic_buffer_delays_product_and_keeps_normal(config_dir):
    """일반 모드 + dynamic_buffer: 정상은 완충 없는 생성과 같고, 입력 펌프 마모는 P-8401A 유량을 서서히(완충) 끌어내린다.
    창을 나눠 생성해도(상태를 이어 감) 한 번에 생성한 것과 같다."""
    plant = load_plant(config_dir / "plant_cdu.yaml")
    base = load_runtime(config_dir / "runtime_cdu.yaml")
    buffered = base.model_copy(update={"dynamic_buffer": config_dir / "dynamic_cdu_buffer.yaml"})
    modes = load_fault_modes(config_dir / "fault_modes.yaml")

    def run(runtime, windows):
        storage = Storage(":memory:")
        seed_master(storage, plant)
        book = AnomalyBook(storage, plant, modes, physics=PlantGenerator(plant, base).physics)
        book.inject("P-8101A", "PM-IMPELLER-WEAR", EPOCH + 60, 10)
        g = PlantGenerator(plant, runtime, book)
        out = []
        for a, b in windows:
            out += g.generate(START + timedelta(seconds=a), START + timedelta(seconds=b))
        storage.close()
        return out

    whole = run(buffered, [(0, 1200)])
    split = run(buffered, [(0, 400), (400, 1200)])
    plain = run(base, [(0, 1200)])
    assert {(s.equip_id, s.tag_id, s.ts): s.value for s in whole} == {(s.equip_id, s.tag_id, s.ts): s.value for s in split}
    at = lambda samples, sec: _values(samples, sec)
    assert at(whole, 50) == at(plain, 50)                                   # 사건 전 정상은 같다
    assert at(whole, 1190)[FLOW] == at(plain, 1190)[FLOW]                    # 입력 펌프 자체는 바꾸지 않는다
    drop = lambda samples, sec: at(samples, 50)[PRODUCT] - at(samples, sec)[PRODUCT]
    assert drop(whole, 1190) > drop(plain, 1190) + 5                         # 고정 분배 완충: 공급 감소가 제품 유량 감소로 이어진다
    assert drop(whole, 400) < drop(whole, 1190)                              # 서서히(완충) 내려간다
