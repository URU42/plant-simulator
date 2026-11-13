"""RFCC 구성안 파일 (설계 §4.1, §7)."""
from common.config_models import load_plant, load_runtime

TEMPS = {"bearing_temp", "suction_temp", "discharge_temp", "oil_temp"}


def test_rfcc_plant_matches_sample_package(rfcc_plant_path):
    plant = load_plant(rfcc_plant_path)
    tags = plant.all_tags()
    assert len(plant.equipment) == 12 and len(tags) == 81
    assert len(plant.unit_ids()) == 8 and len(plant.plant.unit_groups) == 5
    temps = [t for t in tags if t.signal in TEMPS]
    assert len(temps) == 16 and all(t.interval_sec == 10 for t in temps)
    assert all(t.interval_sec == 1 for t in tags if t.signal not in TEMPS)
    assert all(t.limits.warn_lo < t.control.mean < t.limits.warn_hi for t in tags)   # 통계 경고만
    assert all(t.limits.alarm_hi is None and t.limits.trip_hi is None for t in tags)
    by_key = plant.tag_by_key()
    assert (by_key[("P-1101A", "VI-P-1101A")].control.mean, by_key[("P-1101A", "VI-P-1101A")].control.sigma) == (1.4575, 0.03761084502112299)
    assert by_key[("E-1202", "ZI-E-1202")].decimals == 5 and by_key[("B-1201", "FI-B-1201")].decimals == 4
    equip = plant.equipment_by_id()
    assert equip["B-1201"].driven_by == "E-1202" and equip["C-1401"].driven_by == "T-1401"


def test_rfcc_runtime(rfcc_runtime_path, rfcc_plant_path):
    rt = load_runtime(rfcc_runtime_path)
    assert (rt.sample_interval_sec, rt.send_interval_sec, rt.backfill_days, rt.raw_retention_days, rt.seed) == (1, 1, 1, 1, 61109)
    rt.validate_tag_intervals(load_plant(rfcc_plant_path))
