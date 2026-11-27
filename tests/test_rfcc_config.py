"""RFCC 구성안 파일 (설계 §4.1, §7)."""
from common.config_models import load_plant, load_runtime

TEMPS = {"bearing_temp", "suction_temp", "discharge_temp", "oil_temp"}
# 자료 밖 합성 추가 태그: 입구 증기 온도와 2026-11-27 추가 계측 (설명의 합성 표시는 2026-11-27 에 뺐다). P-1101B 는 tag_id 로 거른다
SYNTHETIC_ADDED = {"STT-T-1401",
                   "PDI-P-1101A-STR", "PI-P-1101A-STR", "SI-P-1101A", "PD-P-1101A-D2", "FI-1101-HDR",
                   "PDI-P-1101B-STR", "PI-P-1101B-STR", "SI-P-1101B", "PD-P-1101B-D2",
                   "PDI-P-1303A-STR", "PI-P-1303A-STR", "SI-P-1303A", "ZI-1303-FCV", "PD-P-1303A-D2"}


def test_rfcc_plant_matches_sample_package(rfcc_plant_path):
    plant = load_plant(rfcc_plant_path)
    added = {t.tag_id: t for t in plant.all_tags() if t.tag_id in SYNTHETIC_ADDED}
    assert set(added) == SYNTHETIC_ADDED and all(t.description for t in added.values())
    tags = [t for t in plant.all_tags() if t.tag_id not in SYNTHETIC_ADDED and "1101B" not in t.tag_id]
    assert len(plant.equipment) == 13 and len(tags) == 81 and len(plant.all_tags()) == 102
    assert len(plant.unit_ids()) == 8 and len(plant.plant.unit_groups) == 5
    temps = [t for t in tags if t.signal in TEMPS]
    assert len(temps) == 16 and all(t.interval_sec == 10 for t in temps)
    assert all(t.interval_sec == 1 for t in tags if t.signal not in TEMPS)
    by_key = plant.tag_by_key()
    # 공학 기준값 (2026-11-23, synthetic): 펌프 진동 절대값, 축 진동 ISO 7919-3 회전수 식, 유량 비율, 대기압 가까운 입구는 따로
    def lim(e, t):
        return by_key[(e, t)].limits
    assert (lim("P-1101A", "VI-P-1101A").warn_hi, lim("P-1101A", "VI-P-1101A").alarm_hi, lim("P-1101A", "VI-P-1101A").trip_hi) == (3.0, 4.5, 7.1)
    rpm = by_key[("C-1401", "SI-C-1401")].control.mean
    assert lim("C-1401", "VX-C-1401").alarm_hi == float(f"{9000 / rpm ** 0.5:.3g}")
    assert lim("P-1101A", "FI-P-1101A").warn_lo == float(f"{by_key[('P-1101A', 'FI-P-1101A')].control.mean * 0.766:.3g}")
    assert (lim("B-1201", "PS-B-1201").warn_lo, lim("B-1201", "PS-B-1201").trip_lo) == (-0.05, -0.1)
    assert lim("P-1101A", "FI-P-1101A").warn_hi is None and lim("T-1401", "STT-T-1401").warn_lo == 362.0
    assert (by_key[("P-1101A", "VI-P-1101A")].control.mean, by_key[("P-1101A", "VI-P-1101A")].control.sigma) == (1.4575, 0.03761084502112299)
    assert by_key[("E-1202", "ZI-E-1202")].decimals == 5 and by_key[("B-1201", "FI-B-1201")].decimals == 4
    equip = plant.equipment_by_id()
    assert equip["B-1201"].driven_by == "E-1202" and equip["C-1401"].driven_by == "T-1401"


def test_rfcc_runtime(rfcc_runtime_path, rfcc_plant_path):
    rt = load_runtime(rfcc_runtime_path)
    assert (rt.sample_interval_sec, rt.send_interval_sec, rt.backfill_days, rt.raw_retention_days, rt.seed) == (1, 1, 1, 1, 61109)
    rt.validate_tag_intervals(load_plant(rfcc_plant_path))
