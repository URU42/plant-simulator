"""RFCC 신호 모델 (설계 §4.2): 순수 함수, 자정 연속, 축 속도 공유, 정지 상태."""
from datetime import datetime, timedelta

import numpy as np
import pytest

from common.config_models import ConfigError, load_plant, load_runtime
from common.sim_clock import KST
from simulator.rfcc_model import RFCC_EPOCH, RfccSignalModel, ar1_series, lowpass
from simulator.signal_model import epochs_range

T0 = datetime(2026, 11, 9, 0, 0, tzinfo=KST)


@pytest.fixture(scope="module")
def model(rfcc_plant_path, rfcc_runtime_path):
    rt = load_runtime(rfcc_runtime_path)
    return RfccSignalModel(load_plant(rfcc_plant_path), rt.seed, rt.sample_interval_sec)


def test_ar1_series_is_stationary_with_requested_sd():
    x = ar1_series(np.random.default_rng(1), sd=2.0, tau_sec=6, step_sec=1, n=200_000)
    assert abs(x.std() - 2.0) < 0.05
    a = np.exp(-1 / 6)
    assert abs(np.corrcoef(x[:-1], x[1:])[0, 1] - a) < 0.01


def test_ar1_prefix_is_stable_when_longer_series_requested():
    short = ar1_series(np.random.default_rng(7), 1.0, 1400, 600, 100)
    long = ar1_series(np.random.default_rng(7), 1.0, 1400, 600, 300)
    np.testing.assert_allclose(short, long[:100])


def test_lowpass_has_unit_dc_gain():
    out = lowpass(np.full(2000, 3.0), tau_sec=240, step_sec=10)
    assert np.allclose(out, 3.0, atol=1e-6)


def test_values_do_not_depend_on_chunking(model):
    whole = epochs_range(T0, T0 + timedelta(hours=2), 1)
    for tag in ("VI-P-1101A", "TI-P-1101A", "SI-C-1401", "FI-B-1201"):
        one = model.normal_values(tag, whole)
        two = np.concatenate([model.normal_values(tag, whole[:3600]), model.normal_values(tag, whole[3600:])])
        single = np.array([model.normal_values(tag, whole[i:i + 1])[0] for i in (0, 1234, 7199)])
        np.testing.assert_allclose(one, two)
        np.testing.assert_allclose(one[[0, 1234, 7199]], single)


def test_midnight_is_continuous(model):
    plant = model.plant
    for tag_id in ("VI-P-1101A", "FI-P-1101A", "TI-P-1101A", "SI-B-1201"):
        tag = plant.tag_by_id()[tag_id]
        step = tag.interval_sec or 1
        before = model.normal_values(tag_id, np.array([int(T0.timestamp()) - step]))[0]
        after = model.normal_values(tag_id, np.array([int(T0.timestamp())]))[0]
        assert abs(after - before) < 10 * tag.normal.noise_sd + 1e-9, tag_id


def test_driven_equipment_shares_driver_speed(model):
    epochs = epochs_range(T0, T0 + timedelta(minutes=5), 1)
    np.testing.assert_allclose(model.normal_values("SI-B-1201", epochs), model.normal_values("SI-E-1202", epochs))
    np.testing.assert_allclose(model.normal_values("SI-C-1401", epochs), model.normal_values("SI-T-1401", epochs))
    assert not np.allclose(model.normal_values("SI-B-1201", epochs), model.normal_values("SI-C-1401", epochs))


def test_values_stay_near_baseline_over_a_day(model):
    epochs = epochs_range(T0, T0 + timedelta(days=1), 10)
    by_id = model.plant.tag_by_id()
    for tag_id, tol in (("VI-P-1101A", 0.25), ("TI-P-1101A", 3.0), ("PS-P-1101A", 0.1), ("FI-P-1101A", 15), ("II-P-1101A", 10)):
        v = model.normal_values(tag_id, epochs)
        assert abs(v.mean() - by_id[tag_id].normal.base) < tol, tag_id
        assert np.isfinite(v).all()


def test_stopped_values(model):
    epochs = epochs_range(T0, T0 + timedelta(minutes=1), 1)
    assert abs(model.stopped_values("VI-P-1101A", epochs).mean()) < 0.1
    assert abs(model.stopped_values("TI-P-1101A", epochs).mean() - 25.0) < 1.0
    assert abs(model.stopped_values("ZI-E-1202", epochs).mean() - 0.04) < 0.01
    assert abs(model.stopped_values("SI-C-1401", epochs).mean()) < 1.0


def test_epoch_before_fixed_origin_is_rejected(model):
    with pytest.raises(ConfigError, match="2026-01-01"):
        model.normal_values("VI-P-1101A", np.array([int(RFCC_EPOCH) - 1]))


def test_unknown_signal_kind_and_speed_mismatch_are_reported(rfcc_plant_path, rfcc_runtime_path):
    rt = load_runtime(rfcc_runtime_path)
    plant = load_plant(rfcc_plant_path)
    bad = plant.model_copy(deep=True)
    bad.equipment[0].tags[0].signal = "humidity"
    with pytest.raises(ConfigError, match="unknown signal kind humidity"):
        RfccSignalModel(bad, rt.seed, rt.sample_interval_sec)
    bad = plant.model_copy(deep=True)
    bad.tag_by_id()["SI-B-1201"].normal.base = 5300
    with pytest.raises(ConfigError, match="B-1201"):
        RfccSignalModel(bad, rt.seed, rt.sample_interval_sec)


def test_fast_noise_is_continuous_across_block_boundaries(model):
    """빠른 성분 블록 경계(UTC 자정)의 한 스텝 차이가 블록 안의 한 스텝 차이와 같은 크기여야 한다 (τ 80, τ 12 태그)."""
    by_id = model.plant.tag_by_id()
    for tag_id in ("TI-P-1101A", "ZI-E-1202"):
        step = by_id[tag_id].interval_sec
        boundaries = np.array([int(T0.timestamp()) // 86400 * 86400 + d * 86400 for d in range(30)])     # 30일의 UTC 자정
        at_boundary = np.abs(model.normal_values(tag_id, boundaries) - model.normal_values(tag_id, boundaries - step))
        inside = model.normal_values(tag_id, np.arange(boundaries[0] + 3600, boundaries[0] + 3600 + 3000 * step, step))
        typical = np.abs(np.diff(inside)).mean()
        assert at_boundary.mean() < 1.5 * typical, (tag_id, at_boundary.mean(), typical)
