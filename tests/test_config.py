"""설정 모델 검증: 기획서 9.2 1단계 완료 기준."""
from pathlib import Path

import pytest

from common.config_models import (
    Limits,
    ConfigError,
    PlantConfig,
    RuntimeConfig,
    load_plant,
    load_runtime,
    load_yaml,
)

# RHDS 구성안: 펌프 3, 원심 압축기 2, 팬 1
EXPECTED_TYPE_COUNTS = {"PM": 3, "CC": 2, "FN": 1}


def test_plant_loads_and_matches_master(plant_path: Path):
    plant = load_plant(plant_path)
    assert isinstance(plant, PlantConfig)
    assert plant.plant.plant_id == "PL-RHDS"
    assert len(plant.equipment) == 6
    assert plant.type_counts() == EXPECTED_TYPE_COUNTS
    assert len(plant.all_tags()) == 43
    assert len(plant.unit_ids()) == 6


def test_tag_ids_and_seed_keys_are_unique(plant_path: Path):
    plant = load_plant(plant_path)
    tags = plant.all_tags()
    assert len({t.tag_id for t in tags}) == len(tags)
    assert len({t.seed_key for t in tags}) == len(tags)


def test_control_mean_is_inside_warning_limits(plant_path: Path):
    plant = load_plant(plant_path)
    for tag in plant.all_tags():
        lim = tag.limits
        assert lim.warn_hi is None or tag.control.mean < lim.warn_hi, tag.tag_id
        assert lim.warn_lo is None or tag.control.mean > lim.warn_lo, tag.tag_id


def test_driven_by_references_known_ids(plant_path: Path):
    plant = load_plant(plant_path)
    equip_ids = {e.equip_id for e in plant.equipment}
    for e in plant.equipment:
        if e.driven_by:
            assert e.driven_by in equip_ids


def test_unknown_id_in_driven_by_is_reported_by_name(plant_path: Path):
    raw = load_yaml(plant_path)
    raw["equipment"][0]["driven_by"] = "ST-9999"
    with pytest.raises(ConfigError, match="ST-9999"):
        PlantConfig.model_validate(raw)


def test_runtime_values(runtime_path: Path):
    rt = load_runtime(runtime_path)
    assert isinstance(rt, RuntimeConfig)
    assert rt.units == ["all"]
    assert rt.source_id == "SIM-RHDS"
    assert rt.sample_interval_sec == 1
    assert rt.send_interval_sec == 1
    assert rt.time_mode == "live"
    assert rt.speed == 60
    assert rt.backfill_days == 1
    assert rt.seed == 20261110
    assert rt.collector_url == "http://localhost:8000/rhds"
    assert rt.raw_retention_days == 1


def test_runtime_units_subset_selects_equipment(plant_path: Path):
    plant = load_plant(plant_path)
    rt = RuntimeConfig(units=["UNIT-RHDS-02"], source_id="SIM-U2")
    chosen = plant.equipment_for_units(rt.units)
    assert [e.equip_id for e in chosen] == ["C-3102"]
    assert len(plant.equipment_for_units(["all"])) == 6


def test_runtime_unknown_unit_rejected(plant_path: Path):
    plant = load_plant(plant_path)
    with pytest.raises(ConfigError, match="U-9"):
        plant.equipment_for_units(["U-9"])


def test_sample_interval_must_divide_a_day():
    with pytest.raises(ValueError, match="86400"):
        RuntimeConfig(sample_interval_sec=7, send_interval_sec=70)


# ---------------------------------------------------------------------------
# 구성안 형식: 선택 필드, 필수 관리 기준, 지운 키
# ---------------------------------------------------------------------------

def _minimal():
    """최소 구성안 dict."""
    equip = {"equip_id": "P-1", "name": "pump", "type": "PM", "unit_id": "U-1",
             "tags": [{"tag_id": "TI-P-1", "signal": "bearing_temp", "unit": "degC", "seed_key": "r1",
                       "description": "베어링 온도", "interval_sec": 10, "decimals": 4,
                       "control": {"mean": 66, "sigma": 0.035}}]}
    return {"plant": {"plant_id": "PL", "name": "p", "config_version": 1,
                      "unit_groups": [{"id": "UG-1", "name": "g", "units": [{"id": "U-1", "name": "u"}]}]},
            "equipment_types": [{"type": "PM", "name": "펌프", "prefix": "P", "kind": "pump"}],
            "signals": [{"signal": "bearing_temp", "prefix": "TI", "unit": "degC", "name": "베어링 온도"}],
            "equipment": [equip]}


def test_optional_tag_fields():
    plant = PlantConfig.model_validate(_minimal())
    tag = plant.all_tags()[0]
    assert tag.limits is None and tag.description == "베어링 온도"
    assert tag.decimals == 4 and plant.tag_interval_sec(("P-1", "TI-P-1"), 1) == 10
    assert plant.equipment[0].grade is None
    assert (tag.control.mean, tag.control.sigma) == (66, 0.035)


def test_control_is_required():
    raw = _minimal()
    del raw["equipment"][0]["tags"][0]["control"]
    with pytest.raises(ValueError, match="control"):
        PlantConfig.model_validate(raw)


@pytest.mark.parametrize("where, key, value", [
    ("tag", "normal", {"base": 66, "noise_sd": 0.035}),
    ("tag", "delay_samples", 2),
    ("tag", "location", "DE"),
    ("equipment", "seed_key", "e1"),
    ("plant", "signal_model", "rfcc"),
    ("root", "correlations", []),
])
def test_removed_keys_are_rejected(where, key, value):
    """옛 신호 모델·한계값 알람용 키는 더 받지 않는다 (오타처럼 거부)."""
    raw = _minimal()
    target = {"tag": raw["equipment"][0]["tags"][0], "equipment": raw["equipment"][0], "plant": raw["plant"], "root": raw}[where]
    target[key] = value
    with pytest.raises(ValueError, match=key):
        PlantConfig.model_validate(raw)


def test_tag_interval_must_be_multiple_of_sample_interval():
    plant = PlantConfig.model_validate(_minimal())
    RuntimeConfig(sample_interval_sec=1, send_interval_sec=1).validate_tag_intervals(plant)       # 10 은 1 의 배수
    with pytest.raises(ConfigError, match="TI-P-1"):
        RuntimeConfig(sample_interval_sec=3, send_interval_sec=3).validate_tag_intervals(plant)   # 10 은 3 의 배수가 아니다


# ---------------------------------------------------------------------------
# 복합키 (설비 ID, 태그명), 상·하한 한계값 (다중 공장 설계 §4.1, §4.2)
# ---------------------------------------------------------------------------

def _two_equipment_same_tag():
    raw = _minimal()
    raw["equipment"][0]["tags"][0]["limits"] = {"warn_lo": 3.5}
    raw["equipment"].append({"equip_id": "F-1", "name": "fan", "type": "PM", "unit_id": "U-1",
                             "tags": [{"tag_id": "TI-P-1", "signal": "bearing_temp", "unit": "degC", "seed_key": "r2",
                                       "control": {"mean": 50, "sigma": 0.03}, "normal_ref": "44~60", "limit_basis": "가정값"}]})
    return raw


def test_tag_ids_only_need_to_be_unique_per_equipment():
    plant = PlantConfig.model_validate(_two_equipment_same_tag())
    assert plant.tag_keys() == [("P-1", "TI-P-1"), ("F-1", "TI-P-1")]
    assert plant.tag_by_key()[("F-1", "TI-P-1")].normal_ref == "44~60"
    assert plant.tag_interval_sec(("F-1", "TI-P-1"), 1) == 1 and plant.tag_interval_sec(("P-1", "TI-P-1"), 1) == 10


def test_limits_allow_lower_bounds_and_check_order():
    Limits(warn_lo=2.02, alarm_lo=1.62)
    Limits(alarm_lo=115, alarm_hi=140)
    assert Limits(warn_lo=2.0, alarm_lo=1.5, warn_hi=5.0).lo_levels() == [("Soft", 2.0), ("Alarm", 1.5)]
    with pytest.raises(ValueError):
        Limits(warn_lo=1.5, alarm_lo=2.0)        # 하한은 trip < alarm < warn
    with pytest.raises(ValueError):
        Limits(warn_lo=5.0, warn_hi=4.0)         # lo 는 hi 보다 작다
    with pytest.raises(ValueError):
        Limits()
