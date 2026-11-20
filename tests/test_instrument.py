"""계측 이상 층: 일정의 결정성, 보내는 값만 바뀌는지, 정답지 코드."""
from datetime import datetime, timedelta

import pytest

from collector.anomaly_book import AnomalyBook
from collector.seed_master import seed_master
from collector.storage import Storage
from common.config_models import InstrumentFaults, load_plant, load_runtime
from common.sim_clock import KST
from common.timefmt import to_db
from simulator.fault import load_fault_modes
from simulator.instrument import InstrumentSchedule
from simulator.model import PlantGenerator

T0 = datetime(2026, 11, 12, 0, 0, tzinfo=KST)
DAY = 86400


@pytest.fixture
def rhds(config_dir):
    plant, rt = load_plant(config_dir / "plant_rhds.yaml"), load_runtime(config_dir / "runtime_rhds.yaml")
    on = rt.model_copy(update={"instrument_faults": rt.instrument_faults.model_copy(update={"enabled": True})})
    return plant, rt, on


def test_schedule_is_deterministic_and_off_by_default(rhds):
    plant, rt, on = rhds
    assert not rt.instrument_faults.enabled and PlantGenerator(plant, rt).instrument is None
    t = T0.timestamp()
    a = InstrumentSchedule(plant, on.instrument_faults, on.seed, 1).events(t, t + 7 * DAY)
    b = InstrumentSchedule(plant, on.instrument_faults, on.seed, 1).events(t, t + 7 * DAY)
    c = InstrumentSchedule(plant, on.instrument_faults, on.seed + 1, 1).events(t, t + 7 * DAY)
    assert a == b and a != c
    n = {k: sum(e.kind == k for e in a) for k in ("spike", "stuck", "dropout")}
    tags = len(plant.tag_keys())
    assert 0.8 * 7 * tags < n["spike"] < 1.2 * 7 * tags                  # 태그당 하루 1회
    assert 3 <= n["stuck"] <= 20 and 10 <= n["dropout"] <= 35             # 설비당 주 1회(9대), 공장당 하루 3회
    assert all(4 <= abs(e.z) <= 8 for e in a if e.kind == "spike")


def test_bad_ranges_rejected():
    with pytest.raises(ValueError):
        InstrumentFaults.model_validate({"spike": {"z_min": 9, "z_max": 4}})


def test_only_sent_values_change(rhds):
    """켜도 공정 값은 같다: 계측 이상이 걸린 표본만 다르고(튐 = z·σ, 고착 = 시작 직전 보낸 값, 누락 = 없음) 나머지는 꺼졌을 때와 같다."""
    plant, rt, on = rhds
    end = T0 + timedelta(hours=6)
    off = {(s.equip_id, s.tag_id, s.ts): s.value for s in PlantGenerator(plant, rt).generate(T0, end)}
    got = {(s.equip_id, s.tag_id, s.ts): s.value for s in PlantGenerator(plant, on).generate(T0, end)}
    events = InstrumentSchedule(plant, on.instrument_faults, on.seed, on.sample_interval_sec).events(T0.timestamp(), end.timestamp())
    assert {e.kind for e in events} == {"spike", "stuck", "dropout"}
    ctl = plant.control_by_key()
    touched = set()
    for ev in events:
        for tag in ev.tag_ids:
            keys = sorted(k for k in off if k[:2] == (ev.equip_id, tag) and ev.start <= k[2].timestamp() < ev.end)
            touched |= set(keys)
            if ev.kind == "dropout":
                assert all(k not in got for k in keys)
            elif ev.kind == "spike":
                d = plant.tag_by_key()[keys[0][:2]].decimals
                assert got[keys[0]] == pytest.approx(off[keys[0]] + ev.z * ctl[keys[0][:2]][1], abs=10 ** -d)
            else:
                before = max(k for k in got if k[:2] == keys[0][:2] and k[2] < keys[0][2])
                assert {got[k] for k in keys} == {got[before]}
    assert all(got[k] == v for k, v in off.items() if k not in touched)


def test_small_windows_equal_whole_with_instrument(rhds):
    """고착이 창을 넘어도 같은 값을 붙잡는다."""
    plant, _, on = rhds
    end = T0 + timedelta(hours=6)
    whole = PlantGenerator(plant, on).generate(T0, end)
    g, parts, t = PlantGenerator(plant, on), [], T0
    while t < end:
        parts += g.generate(t, min(t + timedelta(minutes=7), end))
        t += timedelta(minutes=7)
    assert parts == whole


def test_answer_key_codes(rhds, config_dir, tmp_path):
    """정답지 코드: 고장 모드 0, 정상 운전 변화 1, 계측 이상 2. 계측 이상은 이미 만든 구간(sim_now 까지)만."""
    plant, _, on = rhds
    st = Storage(tmp_path / "a.db")
    seed_master(st, plant)
    sched = InstrumentSchedule(plant, on.instrument_faults, on.seed, on.sample_interval_sec)
    book = AnomalyBook(st, plant, load_fault_modes(config_dir / "fault_modes.yaml"), sched)
    t = T0.timestamp()
    book.inject("C-3101", "CC-SURGE", at=t + 100)
    book.inject("M-3102", "OP-LOAD-CHANGE", at=t + 200)
    now = t + 6 * 3600
    st.store_batch("b1", "SIM", to_db(datetime.fromtimestamp(now, KST)),
                   [("P-3101", "VI-A-P3101", to_db(datetime.fromtimestamp(x, KST)), 1.0) for x in (t, now)])
    key = book.answer_key(None, to_db(T0 - timedelta(days=1)), None)         # 데이터보다 앞선 from 은 데이터 시작으로 줄인다
    codes = {e["mode"]: e["code"] for e in key}
    assert codes["CC-SURGE"] == 0 and codes["OP-LOAD-CHANGE"] == 1
    im = [e for e in key if e["code"] == 2]
    expected = [e for e in sched.events(t, now + 1)]
    assert [e["event_id"] for e in im] == [e.event_id for e in expected] and im[0]["kind"] == "계측 이상(설비 고장 아님)"
    assert key == sorted(key, key=lambda e: (e["start_at"], e["event_id"]))
    assert [e["event_id"] for e in book.answer_key("C-3101", to_db(T0), None) if e["code"] == 2] == \
        [e.event_id for e in expected if e.equip_id == "C-3101"]
