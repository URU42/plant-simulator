"""RFCC 구성안 파일 (설계 §4.1, §7)."""
from common.config_models import load_plant, load_runtime

TEMPS = {"bearing_temp", "suction_temp", "discharge_temp", "oil_temp"}


def test_rfcc_plant_matches_sample_package(rfcc_plant_path):
    plant = load_plant(rfcc_plant_path)
    tags = plant.all_tags()
    assert plant.plant.signal_model == "rfcc"
    assert len(plant.equipment) == 12 and len(tags) == 81
    assert len(plant.unit_ids()) == 8 and len(plant.plant.unit_groups) == 5
    temps = [t for t in tags if t.signal in TEMPS]
    assert len(temps) == 16 and all(t.interval_sec == 10 for t in temps)
    assert all(t.interval_sec == 1 for t in tags if t.signal not in TEMPS)
    assert all(t.limits is None for t in tags)
    by_key = plant.tag_by_key()
    assert by_key[("P-1101A", "VI-P-1101A")].normal.base == 1.45 and by_key[("P-1101A", "VI-P-1101A")].normal.noise_sd == 0.03375
    assert by_key[("E-1202", "ZI-E-1202")].decimals == 5 and by_key[("B-1201", "FI-B-1201")].decimals == 4
    equip = plant.equipment_by_id()
    assert equip["B-1201"].driven_by == "E-1202" and equip["C-1401"].driven_by == "T-1401"
    assert plant.equipment_kind("E-1202") == "expander" and plant.equipment_kind("P-1101A") == "pump"


def test_rfcc_runtime(rfcc_runtime_path, rfcc_plant_path):
    rt = load_runtime(rfcc_runtime_path)
    assert (rt.sample_interval_sec, rt.send_interval_sec, rt.backfill_days, rt.raw_retention_days, rt.seed) == (1, 1, 1, 1, 61109)
    rt.validate_tag_intervals(load_plant(rfcc_plant_path))
