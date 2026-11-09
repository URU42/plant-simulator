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

# 요구사항 정의서 M-2: 원심 압축기 4, 왕복동 압축기 2, 펌프 6, 모터 3, 팬 2, 터빈 2, 기어박스 1
EXPECTED_TYPE_COUNTS = {"CC": 4, "RC": 2, "PM": 6, "MT": 3, "FN": 2, "ST": 2, "GB": 1}


def test_plant_loads_and_matches_m2(plant_path: Path):
    plant = load_plant(plant_path)
    assert isinstance(plant, PlantConfig)
    assert plant.plant.plant_id == "PL-HDS"
    assert len(plant.equipment) == 20
    assert plant.type_counts() == EXPECTED_TYPE_COUNTS
    assert len(plant.all_tags()) == 46
    assert len(plant.unit_ids()) == 6


def test_tag_ids_and_seed_keys_are_unique(plant_path: Path):
    plant = load_plant(plant_path)
    tags = plant.all_tags()
    assert len({t.tag_id for t in tags}) == len(tags)
    assert len({t.seed_key for t in tags}) == len(tags)


def test_normal_base_is_below_warn_limit(plant_path: Path):
    plant = load_plant(plant_path)
    for tag in plant.all_tags():
        assert tag.normal.base < tag.limits.warn_hi, tag.tag_id


def test_driven_by_and_correlations_reference_known_ids(plant_path: Path):
    plant = load_plant(plant_path)
    equip_ids = {e.equip_id for e in plant.equipment}
    for e in plant.equipment:
        if e.driven_by:
            assert e.driven_by in equip_ids
    for c in plant.correlations:
        assert plant.tag_unit(plant.resolve(c.source)) == plant.tag_unit(plant.resolve(c.target)), "연관 규칙은 같은 Unit 안에서만"


def test_unknown_id_in_driven_by_is_reported_by_name(plant_path: Path):
    raw = load_yaml(plant_path)
    raw["equipment"][0]["driven_by"] = "ST-9999"
    with pytest.raises(ConfigError, match="ST-9999"):
        PlantConfig.model_validate(raw)


def test_unknown_tag_in_correlation_is_reported_by_name(plant_path: Path):
    raw = load_yaml(plant_path)
    raw["correlations"].append({"source": "VI-C1101", "target": "TI-X9999", "gain": 1.0, "lag_minutes": 1})
    with pytest.raises(ConfigError, match="TI-X9999"):
        PlantConfig.model_validate(raw)


def test_runtime_defaults(runtime_path: Path):
    rt = load_runtime(runtime_path)
    assert isinstance(rt, RuntimeConfig)
    assert rt.units == ["all"]
    assert rt.source_id == "SIM-ALL"
    assert rt.sample_interval_sec == 10
    assert rt.send_interval_sec == 60
    assert rt.time_mode == "live"
    assert rt.speed == 60
    assert rt.backfill_days == 90
    assert rt.seed == 20261108
    assert rt.collector_url == "http://localhost:8000"
    assert rt.raw_retention_days == 7


def test_runtime_units_subset_selects_equipment(plant_path: Path):
    plant = load_plant(plant_path)
    rt = RuntimeConfig(units=["U-1"], source_id="SIM-U1")
    chosen = plant.equipment_for_units(rt.units)
    assert {e.unit_id for e in chosen} == {"U-1"}
    assert len(chosen) == 4
    assert len(plant.equipment_for_units(["all"])) == 20


def test_runtime_unknown_unit_rejected(plant_path: Path):
    plant = load_plant(plant_path)
    with pytest.raises(ConfigError, match="U-9"):
        plant.equipment_for_units(["U-9"])


def test_sample_interval_must_divide_a_day():
    with pytest.raises(ValueError, match="86400"):
        RuntimeConfig(sample_interval_sec=7, send_interval_sec=70)


# ---------------------------------------------------------------------------
# RFCC 구성안을 위한 형식 확장 (설계 §4.1)
# ---------------------------------------------------------------------------

def _rfcc_like(seed_key_on_equipment=True):
    """최소 rfcc 구성안 dict."""
    equip = {"equip_id": "P-1", "name": "pump", "type": "PM", "unit_id": "U-1",
             "tags": [{"tag_id": "TI-P-1", "signal": "bearing_temp", "unit": "degC", "seed_key": "r1",
                       "description": "베어링 온도", "interval_sec": 10, "decimals": 4,
                       "normal": {"base": 66, "noise_sd": 0.035}}]}
    if seed_key_on_equipment:
        equip["seed_key"] = "e1"
    return {"plant": {"plant_id": "PL", "name": "p", "config_version": 1, "signal_model": "rfcc",
                      "unit_groups": [{"id": "UG-1", "name": "g", "units": [{"id": "U-1", "name": "u"}]}]},
            "equipment_types": [{"type": "PM", "name": "펌프", "prefix": "P", "kind": "pump"}],
            "signals": [{"signal": "bearing_temp", "prefix": "TI", "unit": "degC", "name": "베어링 온도"}],
            "equipment": [equip]}


def test_rfcc_fields_are_optional_or_new():
    plant = PlantConfig.model_validate(_rfcc_like())
    tag = plant.all_tags()[0]
    assert plant.plant.signal_model == "rfcc"
    assert tag.limits is None and tag.location is None and tag.description == "베어링 온도"
    assert tag.decimals == 4 and plant.tag_interval_sec(("P-1", "TI-P-1"), 1) == 10
    assert plant.equipment[0].grade is None and plant.equipment_kind("P-1") == "pump"
    assert tag.normal.daily_amp == 0.0 and tag.normal.drift_sd == 0.0


def test_rfcc_requires_equipment_seed_key():
    with pytest.raises(ConfigError, match="equipment P-1: seed_key is required"):
        PlantConfig.model_validate(_rfcc_like(seed_key_on_equipment=False))


def test_tag_interval_must_be_multiple_of_sample_interval():
    plant = PlantConfig.model_validate(_rfcc_like())
    RuntimeConfig(sample_interval_sec=1, send_interval_sec=1).validate_tag_intervals(plant)       # 10 은 1 의 배수
    with pytest.raises(ConfigError, match="TI-P-1"):
        RuntimeConfig(sample_interval_sec=3, send_interval_sec=3).validate_tag_intervals(plant)   # 10 은 3 의 배수가 아니다


def test_hds_plant_still_loads_with_defaults(plant_path):
    plant = load_plant(plant_path)
    assert plant.plant.signal_model == "hds"
    assert all(t.limits is not None and t.decimals == 3 for t in plant.all_tags())
    assert plant.tag_interval_sec(("C-1101", "VI-C1101"), 10) == 10


# ---------------------------------------------------------------------------
# 복합키 (설비 ID, 태그명), TagRef, 상·하한 한계값 (다중 공장 설계 §4.1, §4.2)
# ---------------------------------------------------------------------------

def _two_equipment_same_tag():
    raw = _rfcc_like()
    raw["equipment"][0]["tags"][0]["limits"] = {"warn_lo": 3.5}
    raw["equipment"].append({"equip_id": "F-1", "name": "fan", "type": "PM", "unit_id": "U-1", "seed_key": "e2",
                             "tags": [{"tag_id": "TI-P-1", "signal": "bearing_temp", "unit": "degC", "seed_key": "r2",
                                       "normal": {"base": 50, "noise_sd": 0.03}, "normal_ref": "44~60", "limit_basis": "가정값"}]})
    return raw


def test_tag_ids_only_need_to_be_unique_per_equipment():
    plant = PlantConfig.model_validate(_two_equipment_same_tag())
    assert plant.tag_keys() == [("P-1", "TI-P-1"), ("F-1", "TI-P-1")]
    assert plant.tag_by_key()[("F-1", "TI-P-1")].normal_ref == "44~60"
    assert plant.tag_interval_sec(("F-1", "TI-P-1"), 1) == 1 and plant.tag_interval_sec(("P-1", "TI-P-1"), 1) == 10


def test_resolve_tag_refs():
    plant = PlantConfig.model_validate(_two_equipment_same_tag())
    assert plant.resolve("F-1/TI-P-1") == ("F-1", "TI-P-1")
    assert plant.resolve("TI-P-1", equip_hint="F-1") == ("F-1", "TI-P-1")
    with pytest.raises(ConfigError, match="ambiguous: F-1/TI-P-1, P-1/TI-P-1|ambiguous: P-1/TI-P-1, F-1/TI-P-1"):
        plant.resolve("TI-P-1")
    with pytest.raises(ConfigError, match="X-9/TI-P-1"):
        plant.resolve("X-9/TI-P-1")


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
