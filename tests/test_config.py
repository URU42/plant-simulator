"""설정 모델 검증: 기획서 9.2 1단계 완료 기준."""
from pathlib import Path

import pytest

from common.config_models import (
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
        assert plant.tag_unit(c.source) == plant.tag_unit(c.target), "연관 규칙은 같은 Unit 안에서만"


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
