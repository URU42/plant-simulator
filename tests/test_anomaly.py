import numpy as np
import pytest
from common.config_models import load_plant
from common.nelson import NelsonState, bit, zscore
from simulator.anomaly import AnomalyEvent, Response, primary_tag, resolve, responses, tag_z

def ev(rule, start=0.0, end=None, origin="injected", equip="C-3101", eid="E1"):
    return AnomalyEvent(eid, origin, equip, rule, start, end)

def test_rhds_c3101_response_tags_match_data_v3(config_dir):
    plant = load_plant(config_dir / "plant_rhds.yaml")
    assert primary_tag(plant, "C-3101") == "XI-X-C3101"
    assert set(responses(plant, "C-3101", 1)) == {"XI-X-C3101", "TI-B-C3101", "PI-LO-C3101", "TI-LO-C3101"}
    assert set(responses(plant, "C-3101", 4)) == {"XI-X-C3101", "PI-S-C3101", "PI-D-C3101", "FI-C3101"}
    assert set(responses(plant, "C-3101", 7)) == {"XI-X-C3101"}
    assert responses(plant, "C-3101", 1)["TI-B-C3101"] == Response(2, 1.0, 40, 60)
    assert responses(plant, "C-3101", 1)["PI-LO-C3101"] == Response(1, -1.0, 5, 10)

@pytest.mark.parametrize("key", ["rfcc", "cdu", "hou", "rhds", "vrds"])
def test_every_response_fires_its_rule_after_rounding(config_dir, key):
    plant = load_plant(config_dir / f"plant_{key}.yaml")
    tags = plant.tag_by_key()
    rng = np.random.default_rng(0)
    for e in plant.equipment:
        for rule in range(1, 9):
            for tag_id, resp in responses(plant, e.equip_id, rule).items():
                t = tags[(e.equip_id, tag_id)]
                iv = t.interval_sec or 1
                state, fired = NelsonState(), 0
                for i in range(40):
                    z = tag_z(ev(rule, equip=e.equip_id), resp, resp.lag_sec + i * iv, iv, rng.uniform(-1, 1))
                    v = round(t.control.mean + t.control.sigma * z, t.decimals)
                    fired |= state.push(zscore(v, t.control.mean, t.control.sigma))
                assert fired & bit(resp.rule), (key, e.equip_id, tag_id, rule)

def test_lag_and_recovery():
    r = Response(2, 1.0, 40, 60)
    assert tag_z(ev(2, start=100, end=500), r, 139, 10, 0.0) is None
    assert tag_z(ev(2, start=100, end=500), r, 140, 10, 0.0) == pytest.approx(0.65)
    assert tag_z(ev(2, start=100, end=500), r, 530, 10, 0.0) == pytest.approx(0.65 * 0.5)
    assert tag_z(ev(2, start=100, end=500), r, 560, 10, 0.0) is None
    assert tag_z(ev(7, end=50), Response(7, 1.0, 0, 0), 50, 1, 0.0) is None

def test_resolve_truncates_spontaneous():
    sp = ev(2, start=0, end=1000, origin="spontaneous", eid="SP")
    inj = ev(1, start=300, origin="injected", eid="INJ")
    other = ev(3, start=0, end=1000, origin="spontaneous", equip="P-3101", eid="SP2")
    out = {e.event_id: e for e in resolve([sp, inj, other], {"SP2": 400.0})}
    assert out["SP"].end == 300 and out["INJ"].end is None and out["SP2"].end == 400
    assert "SP3" not in {e.event_id for e in resolve([ev(1, start=500, end=600, origin="spontaneous", eid="SP3"), inj], {})}

def test_resolve_drops_event_stopped_before_start():
    inj = ev(1, start=300, eid="INJ")
    assert resolve([inj], {"INJ": 300.0}) == [] and resolve([inj], {"INJ": 100.0}) == []

def test_resolve_ignores_fault_stopped_before_start_for_cutting():
    sp = ev(2, start=0, end=1000, origin="spontaneous", eid="SP")
    inj = ev(1, start=300, eid="INJ")
    out = resolve([sp, inj], {"INJ": 200.0})
    assert [(e.event_id, e.end) for e in out] == [("SP", 1000)]

def test_resolve_stop_shortens_injected_end():
    out = resolve([ev(1, start=300, eid="INJ")], {"INJ": 500.0})
    assert out[0].end == 500.0
