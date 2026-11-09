"""이상 패턴 모음 (기획서 5.3)."""
import numpy as np
import pytest

from simulator.patterns import PATTERNS, VALUE_PATTERNS, shape

P = np.linspace(0, 1, 11)
EPOCHS = np.arange(0, 110, 10, dtype=np.int64)


def test_all_eight_patterns_registered():
    assert set(PATTERNS) == {"ramp", "step", "spike", "oscillation", "swell", "freeze", "quality", "gap"}
    assert VALUE_PATTERNS == {"ramp", "step", "spike", "oscillation", "swell"}


def test_swell_rises_holds_recovers():
    z = np.array([0.0, 0.125, 0.25, 0.5, 0.7, 0.85, 1.0 - 1e-9])
    out = shape("swell", z, np.zeros_like(z), 2.0)
    np.testing.assert_allclose(out, [0.0, 1.0, 2.0, 2.0, 2.0, 1.0, 0.0], atol=1e-6)


def test_ramp_rises_linearly():
    np.testing.assert_allclose(shape("ramp", P, EPOCHS, 2.0), 2.0 * P)


def test_step_is_constant():
    np.testing.assert_allclose(shape("step", P, EPOCHS, 1.5), 1.5)


def test_spike_jumps_then_decays():
    s = shape("spike", P, EPOCHS, 3.0)
    assert s[0] == pytest.approx(3.0) and s[-1] < 0.1 and np.all(np.diff(s) <= 0)


def test_oscillation_amplitude_grows():
    epochs = np.arange(0, 3600, 10, dtype=np.int64)
    s = shape("oscillation", np.linspace(0, 1, len(epochs)), epochs, 2.0)
    assert np.abs(s[:60]).max() < np.abs(s[-60:]).max() <= 2.0 + 1e-9
