"""사건 장부: 고장 모드 주입·멈춤·정답지."""
from datetime import datetime, timedelta

import pytest

from collector.anomaly_book import AnomalyBook, Busy, NotApplicable
from collector.seed_master import seed_master
from collector.storage import Storage
from common.config_models import load_plant, load_runtime
from common.sim_clock import KST
from common.timefmt import to_db
from simulator.fault import MAX_RECOVERY_SEC, RECOVERY_SEC, load_fault_modes
from simulator.model import PlantGenerator


@pytest.fixture
def book(config_dir, tmp_path):
    plant = load_plant(config_dir / "plant_rhds.yaml")
    st = Storage(tmp_path / "b.db"); seed_master(st, plant)
    modes = load_fault_modes(config_dir / "fault_modes.yaml")
    return AnomalyBook(st, plant, modes), st, plant, modes


def _utc(epoch):
    return to_db(datetime.fromtimestamp(epoch, KST))


def test_inject_busy_and_stop(book):
    b, _, _, _ = book
    e = b.inject("C-3101", "CC-SURGE", at=1000.0)
    assert e.event_id == "FM-PL-RHDS-0001" and e.start == 1000.0 and e.stop is None
    with pytest.raises(Busy):
        b.inject("C-3101", "CC-SURGE", at=1010.0)
    assert b.stop("C-3101", at=1100.0) == (1100.0, [e.event_id])
    assert b.stop("C-3101", at=1200.0)[1] == []
    assert b.inject("C-3101", "CC-SURGE", at=1200.0).event_id == "FM-PL-RHDS-0002"     # 멈춘 뒤에는 다시 걸 수 있다


@pytest.mark.parametrize("equip, mode", [("C-3101", "NO-SUCH"), ("P-3101", "CC-SURGE"), ("C-3102", "CC-SURGE")])
def test_not_applicable(book, equip, mode):
    """모르는 모드, 다른 유형(펌프), 같은 CC 라도 원심이 아닌 왕복동(C-3102)은 걸 수 없다."""
    with pytest.raises(NotApplicable):
        book[0].inject(equip, mode, at=0.0)


def test_applicable_lists_modes_with_tags(book):
    b = book[0]
    assert [m.id for m in b.applicable("C-3101")] == ["CC-SURGE"]
    assert {m.id for m in b.applicable("M-3101")} == {"OP-LOAD-CHANGE"}
    assert {m.id for m in b.applicable("C-3102")} == {"RC-VALVE-LEAK", "RC-LIQUID-INGRESS"}
    assert {m.id for m in b.applicable("GB-3101")} == {"GB-LUBE-WEAR"}


def test_external_includes_recovery_tail(book):
    b = book[0]
    e = b.inject("C-3101", "CC-SURGE", at=100.0)
    b.stop(None, at=200.0)
    assert [x.event_id for x in b.external(0, 101)] == [e.event_id]
    assert b.external(0, 100) == []                                          # 시작 전
    assert [x.stop for x in b.external(200 + MAX_RECOVERY_SEC - 1, 400)] == [200.0]     # 하류 단계의 긴 회복까지
    assert b.external(200 + MAX_RECOVERY_SEC, 400) == []


def test_book_reload_keeps_open_events(book):
    b, st, plant, modes = book
    e = b.inject("C-3101", "CC-SURGE", at=50.0)
    again = AnomalyBook(st, plant, modes)
    assert [x.event_id for x in again.external(0, 100)] == [e.event_id]
    gone = AnomalyBook(st, plant, {})                                        # 파일에서 모드가 빠지면 생성에서만 빠진다
    assert gone.external(0, 100) == [] and len(gone.answer_key(None, None, None)) == 1


def test_answer_key_fields_and_stage_cut(book):
    """멈추기 전에 시작한 단계·태그 효과만 싣는다. 서지 S2 는 120초, S3 는 150초에 시작한다."""
    b = book[0]
    b.inject("C-3101", "CC-SURGE", at=1000.0)
    b.stop("C-3101", at=1130.0)
    row = b.answer_key(None, None, None)[0]
    assert row["mode"] == "CC-SURGE" and row["kind"] == "공정·기계 이상" and row["expect"]["alarms"]
    assert "안티서지 밸브 개도" in row["unobserved"]
    assert [s["id"] for s in row["stages"]] == ["S1", "S2"]
    assert {t["stage"] for t in row["response_tags"]} == {"S1", "S2"}
    assert {(t["tag_id"], t["start_at"]) for t in row["response_tags"] if t["stage"] == "S1"} == {("FI-C3101", _utc(1000.0))}
    assert row["end_at"] == _utc(1130.0 + RECOVERY_SEC)        # 하류 wobble(S2 + 10초)은 멈춤 전에 시작하지 못했다
    assert {(t["via"], t["hop"]) for t in row["response_tags"]} == {("self", 0)}


def test_answer_key_marks_downstream_and_longer_recovery(book):
    """하류 효과가 시작한 사건: response_tags 에 via·hop, end_at 은 가장 먼 단계의 회복(60초 × (k+1))까지."""
    b = book[0]
    b.inject("C-3101", "CC-SURGE", at=1000.0)
    b.stop("C-3101", at=1200.0)
    row = b.answer_key(None, None, None)[0]
    down = {(t["equip_id"], t["via"], t["hop"]) for t in row["response_tags"] if t["equip_id"] != "C-3101"}
    assert down == {("P-3102", "process", 1), ("P-3103", "process", 1)}
    assert row["end_at"] == _utc(1200.0 + 2 * RECOVERY_SEC)


def test_answer_key_skips_zero_length_event(book):
    b = book[0]
    b.inject("C-3101", "CC-SURGE", at=50.0)
    b.stop("C-3101", at=50.0)
    assert b.answer_key(None, None, None) == []


def test_inject_and_stop_never_land_in_generated_samples(book, config_dir):
    """생성기가 시계보다 늦게 재생 중이어도(요청 시각 < 이미 만든 구간 끝) 주입·멈춤은 이미 만든 표본 뒤로 미뤄진다."""
    b, _, plant, _ = book
    rt = load_runtime(config_dir / "runtime_rhds.yaml")
    rt = rt.model_copy(update={"units": [plant.equipment_by_id()["C-3101"].unit_id]})
    t0 = datetime(2026, 11, 12, 0, 0, tzinfo=KST)
    plain = PlantGenerator(plant, rt).generate(t0, t0 + timedelta(seconds=120))
    g = PlantGenerator(plant, rt, b)
    first = g.generate(t0, t0 + timedelta(seconds=60))                       # 장부의 high-water = t0 + 60
    high = t0.timestamp() + 60
    e = b.inject("C-3101", "CC-SURGE", at=t0.timestamp() + 10)              # 이미 만든 구간 안의 요청
    assert e.start == high and b.answer_key("C-3101", None, None)[0]["start_at"] == _utc(high)
    second = g.generate(t0 + timedelta(seconds=60), t0 + timedelta(seconds=120))
    assert first == plain[:len(first)]
    flow = lambda s: [x.value for x in s if x.tag_id == "FI-C3101"]
    assert flow(second)[0] == flow(plain)[60] and flow(second)[1] != flow(plain)[61]   # 시작 시각의 ramp 는 0, 다음 초부터 움직인다
    at, ids = b.stop("C-3101", at=t0.timestamp() + 30)                      # 멈춤도 같은 규칙 (high-water = t0 + 120)
    assert ids == [e.event_id] and at == t0.timestamp() + 120


def test_speed_is_stored_and_scales_answer_key(book, tmp_path):
    """진행 속도 ×10: 정답지의 단계·태그 시작이 1/10 시각, 다시 열어도 속도가 남는다. 모르는 속도는 거절."""
    b, st, plant, modes = book
    t0 = datetime(2026, 11, 12, tzinfo=KST).timestamp()
    e = b.inject("C-3101", "CC-SURGE", at=t0, speed=10)
    assert e.speed == 10
    st.store_batch("b", "T", _utc(t0), [("C-3101", "FI-C3101", _utc(t0 + 600), 1.0)])
    row = next(r for r in b.answer_key("C-3101", None, None) if r["event_id"] == e.event_id)
    assert row["speed"] == 10 and [s["start_at"] for s in row["stages"]] == [_utc(t0), _utc(t0 + 12), _utc(t0 + 15)]   # 0·120·150초 → 0·12·15초
    assert AnomalyBook(st, plant, modes).active()[0].speed == 10
    with pytest.raises(NotApplicable):
        b.inject("P-3101", "PM-SUCTION-LOSS", at=t0, speed=7)
