import numpy as np
import pytest
from common.nelson import QUIET, NelsonState, bit, judge

def bits_of(seq):
    s = NelsonState()
    return [s.push(z) for z in seq]

def test_quiet_pattern_never_fires():
    assert set(bits_of(list(QUIET) * 30)) == {0}

@pytest.mark.parametrize("seq, rule, at", [
    ([3.0001], 1, 0),
    ([0.5] * 9, 2, 8),
    ([0, 0.1, 0.2, 0.3, 0.4, 0.5], 3, 5),
    ([0.5, -0.5] * 7, 4, 13),
    ([2.1, 0.0, 2.1], 5, 2),
    ([1.1, 1.1, 0.0, 1.1, 1.1], 6, 4),
    ([0.5, 0.5, -0.5, -0.5] * 4, 7, 14),
    ([1.5, -1.5, 1.5, 1.5, -1.5, -1.5, 1.5, -1.5], 8, 7),
])
def test_each_rule_fires_on_last_sample(seq, rule, at):
    b = bits_of(seq)
    assert b[at] & bit(rule)
    assert not any(x & bit(rule) for x in b[:at])

@pytest.mark.parametrize("seq, rule", [
    ([3.0], 1),                                  # 경계는 밖이 아니다
    ([0.5] * 4 + [0.0] + [0.5] * 4, 2),          # 0 은 어느 쪽도 아니다
    ([0, 0.1, 0.1, 0.2, 0.3, 0.4], 3),           # 같은 값은 추세를 끊는다
    ([2.0, 0.0, 2.1], 5),
])
def test_boundaries_do_not_fire(seq, rule):
    assert not any(x & bit(rule) for x in bits_of(seq))

def test_peek_does_not_change_state():
    s = NelsonState([0.5] * 8)
    assert s.peek(0.5) & bit(2)
    assert s.peek(-0.5) & bit(2) == 0
    assert s.push(0.5) & bit(2)

def test_judge_matches_state_and_tail():
    rng = np.random.default_rng(1)
    z = np.concatenate([rng.normal(size=3000), np.full(20, 0.6), np.tile([0.7, -0.7], 20),
                        np.linspace(-2.4, 2.4, 20), rng.normal(0, 3, 500)])
    hits, _ = judge(z)
    assert list(hits) == bits_of(z)
    tail_hits, _ = judge(z[100:], tail=z[86:100])
    assert np.array_equal(tail_hits, hits[100:])

def test_judge_low_bits():
    hits, low = judge(np.array([-3.5]))
    assert hits[0] & bit(1) and low[0] & bit(1)
