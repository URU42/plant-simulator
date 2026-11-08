"""정상 신호 모델과 연관 규칙 (기획서 5.1, 5.2, 5.7)."""
from datetime import datetime, timedelta

import numpy as np

from common.config_models import load_plant
from common.sim_clock import KST
from simulator.signal_model import SignalModel, epochs_range

SEED = 20261108
START = datetime(2026, 8, 1, tzinfo=KST)


def _model(plant_path):
    plant = load_plant(plant_path)
    return plant, SignalModel(plant, seed=SEED, sample_interval_sec=10)


def test_seven_days_stay_below_warn_limit(plant_path):
    plant, model = _model(plant_path)
    epochs = epochs_range(START, START + timedelta(days=7), 10)
    assert len(epochs) == 7 * 8640
    for tag in plant.all_tags():
        values = model.values(tag.tag_id, epochs)
        assert values.shape == epochs.shape
        assert values.max() < tag.limits.warn_hi, tag.tag_id
        assert abs(values.mean() - tag.normal.base) < tag.limits.warn_hi * 0.05, tag.tag_id


def test_same_seed_same_values_regardless_of_chunking(plant_path):
    plant, model = _model(plant_path)
    whole = model.values("VI-C1101", epochs_range(START, START + timedelta(hours=3), 10))
    part1 = model.values("VI-C1101", epochs_range(START, START + timedelta(hours=1), 10))
    part2 = model.values("VI-C1101", epochs_range(START + timedelta(hours=1), START + timedelta(hours=3), 10))
    np.testing.assert_array_equal(whole, np.concatenate([part1, part2]))
    other = SignalModel(plant, seed=SEED, sample_interval_sec=10)
    np.testing.assert_array_equal(whole, other.values("VI-C1101", epochs_range(START, START + timedelta(hours=3), 10)))


def test_different_seed_or_tag_gives_different_values(plant_path):
    plant, model = _model(plant_path)
    epochs = epochs_range(START, START + timedelta(hours=1), 10)
    a = model.values("VI-C1101", epochs)
    b = SignalModel(plant, seed=SEED + 1, sample_interval_sec=10).values("VI-C1101", epochs)
    c = model.values("VI-C2101", epochs)
    assert not np.array_equal(a, b)
    assert not np.array_equal(a - a.mean(), c - c.mean())


def test_seed_follows_seed_key_not_tag_id(plant_path):
    """ID 이름만 바꿔도 생성 값이 유지된다 (3.4)."""
    from common.config_models import PlantConfig, load_yaml

    raw = load_yaml(plant_path)
    original = SignalModel(PlantConfig.model_validate(raw), seed=SEED, sample_interval_sec=10)
    raw["equipment"][0]["tags"][0]["tag_id"] = "VI-RENAMED"
    raw["correlations"][0]["source"] = "VI-RENAMED"      # 규칙 파일의 참조도 함께 고친다 (3.4 바꿀 때 고칠 곳)
    renamed = SignalModel(PlantConfig.model_validate(raw), seed=SEED, sample_interval_sec=10)
    epochs = epochs_range(START, START + timedelta(hours=1), 10)
    np.testing.assert_array_equal(original.values("VI-C1101", epochs), renamed.values("VI-RENAMED", epochs))


def test_stopped_equipment_values(plant_path):
    plant, model = _model(plant_path)
    epochs = epochs_range(START, START + timedelta(hours=1), 10)
    assert model.stopped_values("VI-C1101", epochs).max() < 0.5      # 진동은 0 근처
    temp = model.stopped_values("TI-C1101", epochs)
    assert temp.max() < 35 and temp.min() > 15                       # 온도는 주변 온도 쪽
    assert model.stopped_values("PI-C1101", epochs).max() < 0.5      # 압력은 0 근처


def test_correlation_propagates_source_deviation_with_lag(plant_path):
    """VI-C1101 편차 +1.0 → 60분 뒤 TI-C1101 에 gain 1.8 만큼 더해진다."""
    plant, model = _model(plant_path)
    t0 = START + timedelta(hours=2)
    epochs = epochs_range(START, START + timedelta(hours=5), 10)

    def deviation(tag_id: str, ep: np.ndarray) -> np.ndarray:
        if tag_id == "VI-C1101":
            return np.where(ep >= t0.timestamp(), 1.0, 0.0)
        return np.zeros_like(ep, dtype=float)

    plain = model.values("TI-C1101", epochs)
    with_dev = model.values("TI-C1101", epochs, deviation=deviation)
    delta = with_dev - plain
    before = epochs < (t0 + timedelta(minutes=60)).timestamp()
    np.testing.assert_allclose(delta[before], 0.0)
    np.testing.assert_allclose(delta[~before], 1.8)
    # 원천 태그 자신에게는 편차가 그대로 더해진다
    np.testing.assert_allclose(model.values("VI-C1101", epochs, deviation=deviation) - model.values("VI-C1101", epochs),
                               np.where(epochs >= t0.timestamp(), 1.0, 0.0))
