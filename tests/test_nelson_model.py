from datetime import datetime, timedelta
import numpy as np
import pytest
from common.config_models import load_plant, load_runtime
from common.nelson import NelsonState, zscore
from common.sim_clock import KST
from simulator.anomaly import AnomalyEvent
from simulator.nelson_model import NelsonGenerator, NoEvents
from simulator.schedule import daily_events

T0 = datetime(2026, 11, 12, 0, 0, tzinfo=KST)

class Fixed(NoEvents):
    def __init__(self, events, stops=None):
        self.events, self._stops, self.seen = events, stops or {}, []
    def external(self, start, end): return self.events
    def stops(self): return self._stops
    def opened(self, e): self.seen.append(e.event_id)

@pytest.fixture
def rhds(config_dir):
    return load_plant(config_dir / "plant_rhds.yaml"), load_runtime(config_dir / "runtime_rhds.yaml")

def test_normal_is_quiet_and_on_grid(rhds):
    plant, rt = rhds
    s = NelsonGenerator(plant, rt, spontaneous=False).generate(T0, T0 + timedelta(hours=2))
    assert sum(x.status == "Bad" for x in s) / len(s) < 0.005
    tags = plant.tag_by_key()
    assert all(round(x.value, tags[(x.equip_id, x.tag_id)].decimals) == x.value for x in s)

def test_injected_event_marks_only_its_equipment(rhds):
    plant, rt = rhds
    t = T0.timestamp() + 60
    g = NelsonGenerator(plant, rt, Fixed([AnomalyEvent("I1", "injected", "C-3101", 1, t, None)]), spontaneous=False)
    s = g.generate(T0, T0 + timedelta(minutes=5))
    bad = {(x.equip_id, x.tag_id) for x in s if x.status == "Bad" and x.ts.timestamp() >= t}
    assert ("C-3101", "XI-X-C3101") in bad
    assert all(e == "C-3101" for e, _ in bad)
    assert next(x for x in s if x.tag_id == "XI-X-C3101" and x.ts.timestamp() == t).status == "Bad"

def test_stop_returns_equipment_to_normal(rhds):
    plant, rt = rhds
    t = T0.timestamp() + 60
    g = NelsonGenerator(plant, rt, Fixed([AnomalyEvent("I1", "injected", "C-3101", 6, t, t + 120)]), spontaneous=False)
    s = g.generate(T0, T0 + timedelta(minutes=10))
    assert not [x for x in s if x.equip_id == "C-3101" and x.status == "Bad" and x.ts.timestamp() >= t + 120 + 60 + 150]

def test_spontaneous_union_ratio_near_ten_percent(rhds):
    plant, rt = rhds
    rt = rt.model_copy(update={"units": [plant.equipment_by_id()["C-3101"].unit_id]})
    src = Fixed([])
    s = NelsonGenerator(plant, rt, src).generate(T0, T0 + timedelta(days=1))
    iv = {k: (t.interval_sec or 1) for k, t in plant.tag_by_key().items()}
    for equip in {x.equip_id for x in s}:
        held = np.zeros(86400, dtype=bool)
        for x in s:
            if x.equip_id == equip and x.status == "Bad":
                i = int(x.ts.timestamp() - T0.timestamp()); held[i:i + iv[(x.equip_id, x.tag_id)]] = True
        assert 0.09 <= held.mean() <= 0.11, equip
    assert len(src.seen) == len(set(src.seen)) > 0

def test_restore_seam_has_no_violation(rhds):
    plant, rt = rhds
    a = NelsonGenerator(plant, rt, spontaneous=False)
    first = a.generate(T0, T0 + timedelta(minutes=10))
    last = {}
    for x in first:
        last.setdefault((x.equip_id, x.tag_id), []).append(x.value)
    b = NelsonGenerator(plant, rt, spontaneous=False)
    b.restore({k: v[-14:] for k, v in last.items()})
    nxt = b.generate(T0 + timedelta(minutes=10), T0 + timedelta(minutes=11))
    assert not [x for x in nxt if x.status == "Bad"]

def test_deterministic(rhds):
    plant, rt = rhds
    run = lambda: NelsonGenerator(plant, rt).generate(T0, T0 + timedelta(minutes=3))
    assert run() == run()

def test_rfcc_driven_speed_equal(config_dir):
    plant, rt = load_plant(config_dir / "plant_rfcc.yaml"), load_runtime(config_dir / "runtime_rfcc.yaml")
    s = NelsonGenerator(plant, rt, spontaneous=False).generate(T0, T0 + timedelta(minutes=2))
    speed = lambda eq: [x.value for x in s if x.equip_id == eq and plant.tag_by_key()[(eq, x.tag_id)].signal == "speed"]
    assert speed("B-1201") == speed("E-1202") and speed("C-1401") == speed("T-1401")

class Overlapping(Fixed):
    """요청한 [start, end) 와 겹치는 사건만 돌려주는 소스 (계약의 최소 구현)."""
    def external(self, start, end):
        return [e for e in self.events if e.start < end and (e.end is None or e.end > start)]

def test_cut_spontaneous_stays_cut_across_small_windows(rhds):
    plant, rt = rhds
    rt = rt.model_copy(update={"units": [plant.equipment_by_id()["P-3101"].unit_id]})
    sp = daily_events(rt.seed, "P-3101", T0.date())[0]      # 562초 규칙 1 사건
    S = sp.start
    fault = AnomalyEvent("I1", "injected", "P-3101", 6, S + 50, S + 150)
    a, b = datetime.fromtimestamp(S - 10, KST), datetime.fromtimestamp(S + 600, KST)
    whole = NelsonGenerator(plant, rt, Overlapping([fault])).generate(a, b)
    g, split = NelsonGenerator(plant, rt, Overlapping([fault])), []
    for k in range(610):
        split += g.generate(a + timedelta(seconds=k), a + timedelta(seconds=k + 1))
    assert split == whole
    assert not [x for x in split if x.equip_id == "P-3101" and x.status == "Bad"
                and S + 150 + 60 + 150 <= x.ts.timestamp() < sp.end]
