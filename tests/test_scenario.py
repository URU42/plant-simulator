"""시나리오 적용 (기획서 5.4~5.6): 이상 패턴, 연관 전파, 정비, 우연한 잡음 알람."""
from datetime import datetime, timedelta

import numpy as np
import pytest

from common.config_models import ConfigError, RuntimeConfig, load_plant, load_runtime
from common.scenario_models import ScenarioConfig, ScenarioEvent, parse_duration
from common.sim_clock import KST
from simulator.generator import SampleGenerator
from simulator.random_anomalies import RandomAnomalyMaker
from simulator.scenario import Scenario, load_scenarios
from simulator.signal_model import epochs_range

T0 = datetime(2026, 10, 20, 0, 0, tzinfo=KST)


def _scenario(plant, **overrides):
    raw = {"events": [], "maintenance": [], "random_anomalies": {"rate_per_tag_day": 0.0}} | overrides
    return Scenario([ScenarioConfig.model_validate(raw)], plant, RuntimeConfig())


def _ramp_event(**kw):
    return {"event_id": "EV-1", "tag_id": "VI-C1101", "pattern": "ramp", "start": T0.isoformat(), "duration": "2d",
            "magnitude": 3.0} | kw


def test_parse_duration():
    assert parse_duration("5d") == timedelta(days=5)
    assert parse_duration("12h") == timedelta(hours=12)
    assert parse_duration("30m") == timedelta(minutes=30)
    assert parse_duration("90s") == timedelta(seconds=90)
    with pytest.raises(ValueError):
        parse_duration("5 weeks")


def test_unknown_ids_are_reported_by_name(plant_path):
    plant = load_plant(plant_path)
    with pytest.raises(ConfigError, match="VI-NOPE"):
        _scenario(plant, events=[_ramp_event(tag_id="VI-NOPE")])
    with pytest.raises(ConfigError, match="TI-NOPE"):
        _scenario(plant, events=[_ramp_event(related=[{"tag_id": "TI-NOPE", "ratio": 1.0}])])
    with pytest.raises(ConfigError, match="X-9"):
        _scenario(plant, maintenance=[{"work_order_id": "WO", "equip_id": "X-9", "start": T0.isoformat(), "end": (T0 + timedelta(hours=1)).isoformat()}])


def test_ramp_raises_tag_and_correlated_temperature_follows(plant_path):
    plant = load_plant(plant_path)
    sc = _scenario(plant, events=[_ramp_event()])
    model = sc.model_for_tests()
    equip = plant.equipment_by_id()["C-1101"]
    epochs = epochs_range(T0 - timedelta(hours=1), T0 + timedelta(days=2, hours=2), 10)
    vi, q, present = sc.apply(model, equip, "VI-C1101", epochs)
    ti, _, _ = sc.apply(model, equip, "TI-C1101", epochs)
    plain_vi = model.values("VI-C1101", epochs)
    plain_ti = model.values("TI-C1101", epochs)
    end = (T0 + timedelta(days=2)).timestamp()
    assert np.allclose(vi[epochs < T0.timestamp()], plain_vi[epochs < T0.timestamp()])
    last = end - 10                                                            # 구간은 [start, end) 반열림
    assert (vi - plain_vi)[epochs == last][0] == pytest.approx(3.0, abs=1e-3)
    assert np.allclose(vi[epochs >= end], plain_vi[epochs >= end])            # recovered_at 이 없으면 duration 뒤 정상
    assert (ti - plain_ti)[epochs == last + 3600][0] == pytest.approx(1.8 * 3.0, abs=1e-2)   # 60분 뒤 gain 1.8
    assert all(x == "Good" for x in q) and present.all()


def test_event_holds_until_recovered_at_then_returns_to_normal(plant_path):
    plant = load_plant(plant_path)
    recovered = T0 + timedelta(days=4)
    sc = _scenario(plant, events=[_ramp_event(recovered_at=recovered.isoformat())])
    model = sc.model_for_tests()
    equip = plant.equipment_by_id()["C-1101"]
    epochs = epochs_range(T0 + timedelta(days=3), T0 + timedelta(days=5), 10)
    vi, _, _ = sc.apply(model, equip, "VI-C1101", epochs)
    delta = vi - model.values("VI-C1101", epochs)
    assert np.allclose(delta[epochs < recovered.timestamp()], 3.0)
    assert np.allclose(delta[epochs >= recovered.timestamp()], 0.0)


def test_related_and_spread_to_driven(plant_path):
    plant = load_plant(plant_path)
    ev = _ramp_event(tag_id="VI-M4101", pattern="step", magnitude=1.0,
                     related=[{"tag_id": "VI-P4101", "ratio": 0.5}], spread_to_driven=0.6)
    sc = _scenario(plant, events=[ev])
    model = sc.model_for_tests()
    by_id = plant.equipment_by_id()
    epochs = epochs_range(T0 + timedelta(hours=1), T0 + timedelta(hours=2), 10)
    for tag_id, equip_id, expected in [("VI-M4101", "M-4101", 1.0), ("VI-P4101", "P-4101", 0.5), ("VI-G4101", "G-4101", 0.6)]:
        v, _, _ = sc.apply(model, by_id[equip_id], tag_id, epochs)
        assert np.allclose(v - model.values(tag_id, epochs), expected), tag_id
    v, _, _ = sc.apply(model, by_id["C-4101"], "VI-C4101", epochs)      # 두 단계 건너서는 번지지 않는다
    assert np.allclose(v - model.values("VI-C4101", epochs), 0.0)


def test_freeze_quality_gap_patterns(plant_path):
    plant = load_plant(plant_path)
    sc = _scenario(plant, events=[
        _ramp_event(event_id="F", tag_id="TI-P4101", pattern="freeze", duration="1h"),
        _ramp_event(event_id="Q", tag_id="TI-C6101", pattern="quality", duration="1h", quality="ScanOff"),
        _ramp_event(event_id="G", tag_id=None, equip_id="P-6101", pattern="gap", duration="1h"),
    ])
    model = sc.model_for_tests()
    by_id = plant.equipment_by_id()
    epochs = epochs_range(T0, T0 + timedelta(hours=1), 10)
    frozen, _, _ = sc.apply(model, by_id["P-4101"], "TI-P4101", epochs)
    assert np.ptp(frozen) == 0.0
    _, q, _ = sc.apply(model, by_id["C-6101"], "TI-C6101", epochs)
    assert set(q) == {"ScanOff"}
    for tag_id in ("VI-P6101", "TI-P6101"):
        _, _, present = sc.apply(model, by_id["P-6101"], tag_id, epochs)
        assert not present.any()
    samples = SampleGenerator(plant, RuntimeConfig(), sc).generate(T0, T0 + timedelta(minutes=1))
    assert not any(s.equip_id == "P-6101" for s in samples)
    assert all(s.value is None for s in samples if s.tag_id == "TI-C6101")


def test_maintenance_stops_equipment(plant_path):
    plant = load_plant(plant_path)
    sc = _scenario(plant, maintenance=[{"work_order_id": "WO-1", "equip_id": "C-1101", "start": T0.isoformat(),
                                        "end": (T0 + timedelta(hours=6)).isoformat(), "description": "베어링 교체"}])
    model = sc.model_for_tests()
    equip = plant.equipment_by_id()["C-1101"]
    epochs = epochs_range(T0 - timedelta(hours=1), T0 + timedelta(hours=7), 10)
    vi, _, _ = sc.apply(model, equip, "VI-C1101", epochs)
    inside = (epochs >= T0.timestamp()) & (epochs < (T0 + timedelta(hours=6)).timestamp())
    assert vi[inside].max() < 0.5 and vi[~inside].min() > 1.5
    ti, _, _ = sc.apply(model, equip, "TI-C1101", epochs)
    assert ti[inside].max() < 30


def test_random_anomalies_are_deterministic_and_sparse(plant_path):
    plant = load_plant(plant_path)
    sc = _scenario(plant, random_anomalies={"rate_per_tag_day": 0.5})
    model = sc.model_for_tests()
    equip = plant.equipment_by_id()["C-1101"]
    epochs = epochs_range(T0, T0 + timedelta(days=10), 10)
    v1, _, _ = sc.apply(model, equip, "VI-C1101", epochs)
    v2, _, _ = Scenario(sc.configs, plant, RuntimeConfig()).apply(model, equip, "VI-C1101", epochs)
    np.testing.assert_array_equal(v1, v2)
    exceed = (v1 > 4.5).sum()
    assert 0 < exceed < len(epochs) * 0.1
    quiet = _scenario(plant)
    assert not (quiet.apply(model, equip, "VI-C1101", epochs)[0] > 4.5).any()


def test_dev_scenario_file_loads_and_matches_plant(plant_path, scenario_path):
    plant = load_plant(plant_path)
    sc = load_scenarios([scenario_path], plant, RuntimeConfig())
    cfg = sc.configs[0]
    assert len(cfg.events) >= 7
    assert {e.pattern for e in cfg.events} == {"ramp", "step", "spike", "oscillation", "freeze", "quality", "gap"}
    assert any(e.recovered_at and e.work_order_id for e in cfg.events)
    assert {m.work_order_id for m in cfg.maintenance} >= {e.work_order_id for e in cfg.events if e.work_order_id}


# ---------------------------------------------------------------------------
# RFCC 시나리오: swell 반복 이벤트, 품질 이벤트 반복, 한계값 없는 태그 (설계 §4.3)
# ---------------------------------------------------------------------------

def test_repeat_every_repeats_the_event_weekly(rfcc_plant_path, rfcc_runtime_path, rfcc_scenario_path):
    plant, runtime = load_plant(rfcc_plant_path), load_runtime(rfcc_runtime_path)
    sc = load_scenarios([str(rfcc_scenario_path)], plant, runtime)
    first = datetime(2026, 11, 2, 5, 10, tzinfo=KST)
    e1 = epochs_range(first, first + timedelta(minutes=40), 1)
    e2 = e1 + 7 * 86400
    np.testing.assert_allclose(sc.deviation("VI-P-1101A", e1), sc.deviation("VI-P-1101A", e2))
    assert sc.deviation("VI-P-1101A", e1).max() > 1.8                                   # magnitude 1.85
    assert sc.deviation("PS-P-1101A", e1).min() < -0.8                                  # ratio −0.4595
    assert sc.deviation("VI-P-1101A", e1 + 3 * 86400).max() == 0.0                      # 회차 사이는 0


def test_repeat_every_rejects_recovered_at():
    with pytest.raises(ValueError, match="repeat_every cannot be combined"):
        ScenarioEvent(event_id="E", tag_id="X", pattern="swell", start=datetime(2026, 1, 1, tzinfo=KST), duration="1h",
                      repeat_every="7d", recovered_at=datetime(2026, 1, 2, tzinfo=KST))


def test_quality_event_repeats(rfcc_plant_path, rfcc_runtime_path, rfcc_scenario_path):
    plant, runtime = load_plant(rfcc_plant_path), load_runtime(rfcc_runtime_path)
    sc = load_scenarios([str(rfcc_scenario_path)], plant, runtime)
    equip = plant.equipment_by_id()["P-1302A"]
    start = datetime(2026, 11, 2, 23, 13, 20, tzinfo=KST) + timedelta(days=14)
    epochs = epochs_range(start - timedelta(seconds=5), start + timedelta(seconds=65), 1)
    _, qualities, _ = sc.apply(sc.model_for_tests(), equip, "VI-P-1302A", epochs)
    assert qualities[:5] == ["Good"] * 5 and qualities[5:65] == ["Bad"] * 60 and qualities[65:] == ["Good"] * 5


def test_random_anomalies_skip_tags_without_limits(rfcc_plant_path, rfcc_runtime_path):
    plant, runtime = load_plant(rfcc_plant_path), load_runtime(rfcc_runtime_path)
    maker = RandomAnomalyMaker(plant, runtime.seed, rate_per_tag_day=5.0)
    epochs = epochs_range(datetime(2026, 11, 9, tzinfo=KST), datetime(2026, 11, 10, tzinfo=KST), 60)
    assert maker.offset("VI-P-1101A", epochs).max() == 0.0
