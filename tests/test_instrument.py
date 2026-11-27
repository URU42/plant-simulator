"""계측 이상 층: 일정의 결정성, 보내는 값만 바뀌는지, 정답지 코드."""
import hashlib
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


# plant_id 접두사 제거(2026-11-27) 전 origin/main 1430d8f 코드로 뽑은 2026-11-12 하루 일정: (사건 수, (종류·설비·태그·시작·끝·z) 해시)
BEFORE_PREFIX_REMOVAL = {"rfcc": (93, "9920c7e71292ece9"), "cdu": (48, "5218b119e88cf037"), "hou": (58, "096ddfa2fc316bea"),
                         "rhds": (69, "5d3e5468854c36ff"), "vrds": (61, "2403a761cf0230a9")}


@pytest.mark.parametrize("key", BEFORE_PREFIX_REMOVAL)
def test_schedule_same_after_plant_id_prefix_removal(key, config_dir):
    plant, rt = load_plant(config_dir / f"plant_{key}.yaml"), load_runtime(config_dir / f"runtime_{key}.yaml")
    t = T0.timestamp()
    ev = InstrumentSchedule(plant, rt.instrument_faults.model_copy(update={"enabled": True}), rt.seed,
                            rt.sample_interval_sec).events(t, t + DAY)
    sig = [(e.kind, e.equip_id, e.tag_ids, e.start, e.end, round(e.z, 9)) for e in ev]
    assert (len(ev), hashlib.sha256(repr(sig).encode()).hexdigest()[:16]) == BEFORE_PREFIX_REMOVAL[key]
    assert ev[0].event_id == f"IM-{key.upper()}-20261112-0001"          # ID 만 새 형식


def test_bad_ranges_rejected():
    with pytest.raises(ValueError):
        InstrumentFaults.model_validate({"spike": {"z_min": 9, "z_max": 4}})


def test_only_sent_values_change(rhds):
    """켜도 공정 값은 같다: 계측 이상이 걸린 표본만 다르고(튐 = z·σ, 고착 = 시작 직전 보낸 값, 누락 = 없음) 나머지는 꺼졌을 때와 같다."""
    plant, rt, on = rhds
    start = T0 + timedelta(hours=34)       # 세 종류가 모두 있고 서로 겹치지 않는 6시간 (태그가 늘면 일정이 바뀐다. 2026-11-27 48 → 34)
    end = start + timedelta(hours=6)
    off = {(s.equip_id, s.tag_id, s.ts): s.value for s in PlantGenerator(plant, rt).generate(start, end)}
    got = {(s.equip_id, s.tag_id, s.ts): s.value for s in PlantGenerator(plant, on).generate(start, end)}
    events = InstrumentSchedule(plant, on.instrument_faults, on.seed, on.sample_interval_sec).events(start.timestamp(), end.timestamp())
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


def test_bias_and_drift_change_only_their_tag(rhds):
    """bias 는 구간 내내 z·σ, drift 는 0 → z·σ 선형. 다른 태그·구간 밖은 그대로다(평가 배치 전용 종류)."""
    from simulator.instrument import InstrumentEvent

    class Fixed:
        def __init__(self, items):
            self.items = items

        def events(self, s, e):
            return [x for x in self.items if x.start < e and x.end > s]

    plant, rt, _ = rhds
    eq = plant.equipment[0]
    t1, t2 = eq.tags[0].tag_id, eq.tags[1].tag_id
    start, end = T0, T0 + timedelta(minutes=20)
    s0 = int(start.timestamp()) + 300
    events = [InstrumentEvent("B", "bias", eq.equip_id, (t1,), s0, s0 + 600, 5.0),
              InstrumentEvent("D", "drift", eq.equip_id, (t2,), s0, s0 + 600, -8.0)]
    off = {(s.equip_id, s.tag_id, s.ts): s.value for s in PlantGenerator(plant, rt).generate(start, end)}
    g = PlantGenerator(plant, rt)
    g.instrument = Fixed(events)
    got = {(s.equip_id, s.tag_id, s.ts): s.value for s in g.generate(start, end)}
    ctl, tags = plant.control_by_key(), plant.tag_by_key()
    for ev in events:
        key = (ev.equip_id, ev.tag_ids[0])
        sd, tol = ctl[key][1], 10 ** -tags[key].decimals
        inside = [k for k in off if k[:2] == key and ev.start <= k[2].timestamp() < ev.end]
        assert inside
        for k in inside:
            frac = 1.0 if ev.kind == "bias" else (k[2].timestamp() - ev.start) / (ev.end - ev.start)
            assert got[k] == pytest.approx(off[k] + ev.z * sd * frac, abs=tol)
    touched = {k for ev in events for k in off if k[:2] == (ev.equip_id, ev.tag_ids[0]) and ev.start <= k[2].timestamp() < ev.end}
    assert all(got[k] == v for k, v in off.items() if k not in touched)
