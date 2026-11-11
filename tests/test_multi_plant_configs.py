"""CDU · HOU · RHDS · VRDS 구성안이 자료의 마스터 표와 같다 (다중 공장 설계 §5.1)."""
import pytest

from common.config_models import load_plant, load_runtime
from common.scenario_models import load_scenario

EXPECTED = {"cdu": ("PL-CDU", 7, 46), "hou": ("PL-HOU", 7, 61), "rhds": ("PL-RHDS", 6, 43), "vrds": ("PL-VRDS", 6, 43)}


@pytest.mark.parametrize("key", list(EXPECTED))
def test_plant_counts_and_files_load(key, config_dir):
    plant = load_plant(config_dir / f"plant_{key}.yaml")
    plant_id, n_equip, n_tags = EXPECTED[key]
    assert plant.plant.plant_id == plant_id and plant.plant.signal_model == "rfcc"
    assert len(plant.equipment) == n_equip and len(plant.tag_keys()) == n_tags
    rt = load_runtime(config_dir / f"runtime_{key}.yaml")
    assert rt.collector_url.endswith(f"/{key}") and rt.sample_interval_sec == 1
    rt.validate_tag_intervals(plant)
    load_scenario(config_dir / f"scenario_{key}.yaml", plant)


def test_master_values_match_excel_samples(config_dir):
    cdu = load_plant(config_dir / "plant_cdu.yaml").tag_by_key()
    t = cdu[("P-8101A", "PI-8101A")]
    assert (t.limits.warn_lo, t.limits.alarm_lo, t.limits.warn_hi) == (2.02, 1.62, None)
    assert t.unit == "kg/cm2(g)" and t.normal_ref == "2.38~3.22" and t.interval_sec == 1 and abs(t.normal.base - 2.79) < 0.05
    hou = load_plant(config_dir / "plant_hou.yaml").tag_by_key()
    z = hou[("C-2301", "ZI-2301")]
    assert (z.limits.warn_hi, z.limits.alarm_lo, z.limits.alarm_hi, z.limits.trip_lo, z.limits.trip_hi) == (0.2, -0.3, 0.3, -0.5, 0.5)
    assert hou[("C-2301", "TI-2301A")].interval_sec == 10 and hou[("C-2301", "TI-2301A")].description == "구동측 저널 베어링 패드 온도"
    rhds = load_plant(config_dir / "plant_rhds.yaml")
    assert [k for k in rhds.tag_keys() if k[1].startswith("TI-B-") and k[1].endswith("3101")] == [
        ("P-3101", "TI-B-P3101"), ("C-3101", "TI-B-C3101"), ("F-3101", "TI-B-F3101")]   # 자료의 TI-B-3101 셋
    s = rhds.tag_by_key()[("C-3101", "SI-C3101")]
    assert (s.limits.warn_lo, s.limits.warn_hi) == (8500, 10000) and s.signal == "speed"
    assert rhds.tag_by_key()[("F-3101", "PDI-F3101")].signal == "differential_pressure"
    assert rhds.tag_by_key()[("P-3102", "TI-F-P3102")].signal == "process_temp"
    vrds = load_plant(config_dir / "plant_vrds.yaml")
    assert load_runtime(config_dir / "runtime_vrds.yaml").seed == 20261111
    assert {e.seed_key for e in vrds.equipment}.isdisjoint({e.seed_key for e in rhds.equipment})



@pytest.mark.parametrize("key", ["rhds", "vrds"])
def test_rhds_vrds_alarm_trip_follow_cdu_hou_gaps(key, config_dir):
    """경고는 자료 그대로, 알람·정지는 CDU·HOU 의 같은 신호 간격 (차압 하한·속도 하한만 정지 없음)."""
    tags = load_plant(config_dir / f"plant_{key}.yaml").tag_by_key()
    n = {"rhds": "3", "vrds": "4"}[key]                                                  # 설비 번호대
    vib = tags[(f"P-{n}101", f"VI-A-P{n}101")].limits
    assert (vib.warn_hi, vib.alarm_hi, vib.trip_hi) == (4.5, 7.1, 11)                    # ISO 20816 경계
    tb = tags[(f"P-{n}101", f"TI-B-P{n}101")].limits
    assert (tb.warn_hi, tb.alarm_hi, tb.trip_hi) == (85, 95, 105)
    ps = tags[(f"P-{n}101", f"PI-S-P{n}101")].limits
    assert (ps.warn_lo, ps.alarm_lo, ps.trip_lo) == (3.5, 2.8, 2.24)
    assert all(t.limits.alarm_hi is not None or t.limits.alarm_lo is not None for t in tags.values())


@pytest.mark.parametrize("key", ["rfcc", "cdu", "hou", "rhds", "vrds"])
def test_tag_names_unique_within_plant(key, config_dir):
    """태그명은 공장 안에서 고유하다. RHDS·VRDS 는 자료의 겹치는 이름에 설비 종류 문자를 넣어 바꿨다."""
    names = [tag for _, tag in load_plant(config_dir / f"plant_{key}.yaml").tag_keys()]
    assert len(names) == len(set(names))


def test_plants_yaml_lists_five(config_dir):
    """배포 공장 목록: HDS 는 RHDS·VRDS 로 대체돼 뺐다 (k8s 설계 §4.3). 파일과 테스트는 남는다."""
    from collector.plants import load_plants
    assert [p.key for p in load_plants(config_dir / "plants.yaml").plants] == ["rfcc", "cdu", "hou", "rhds", "vrds"]
    k8s = load_plants(config_dir / "plants.k8s.yaml").plants
    assert [p.key for p in k8s] == ["rfcc", "cdu", "hou", "rhds", "vrds"] and all(p.db.startswith("/app/data/") for p in k8s)


def test_normal_noise_leaves_margin_below_warning_limits(config_dir):
    """정상 태그의 잡음이 경고 한계에 닿을 만큼 크면 안 된다: |한계 − 기준값| ≥ 6·noise_sd (리뷰 Important 4)."""
    worst = []
    for key in ("cdu", "hou", "rhds", "vrds"):
        for (equip_id, tag_id), t in load_plant(config_dir / f"plant_{key}.yaml").tag_by_key().items():
            if t.limits is None:
                continue
            for limit in (t.limits.warn_hi, t.limits.warn_lo, t.limits.alarm_hi, t.limits.alarm_lo):
                if limit is not None:
                    worst.append((abs(limit - t.normal.base) / t.normal.noise_sd, key, equip_id, tag_id))
    z, *where = min(worst)
    assert z >= 6, (z, *where)
