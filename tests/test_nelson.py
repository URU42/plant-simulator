import pytest
from common.nelson import HISTORY, QUIET, NelsonState, bit

def bits_of(seq):
    s = NelsonState()
    return [s.push(z) for z in seq]

def test_quiet_pattern_never_fires():
    assert set(bits_of(list(QUIET) * 30)) == {0}

@pytest.mark.parametrize("seq, rule, at", [
    ([3.0001], 1, 0),
    ([-3.0001], 1, 0),
    ([0.5] * 9, 2, 8),
    ([-0.5] * 9, 2, 8),
    ([0, 0.1, 0.2, 0.3, 0.4, 0.5], 3, 5),
    ([0.5, 0.4, 0.3, 0.2, 0.1, 0], 3, 5),
])
def test_each_rule_fires_on_last_sample(seq, rule, at):
    b = bits_of(seq)
    assert b[at] & bit(rule)
    assert not any(x & bit(rule) for x in b[:at])

@pytest.mark.parametrize("seq, rule", [
    ([3.0], 1),                                  # 경계는 밖이 아니다
    ([0.5] * 4 + [0.0] + [0.5] * 4, 2),          # 0 은 어느 쪽도 아니다
    ([0, 0.1, 0.1, 0.2, 0.3, 0.4], 3),           # 같은 값은 추세를 끊는다
])
def test_boundaries_do_not_fire(seq, rule):
    assert not any(x & bit(rule) for x in bits_of(seq))

def test_only_rules_one_to_three():
    """규칙 4~8 모양(교대·2σ 밖 2/3·고착)은 이제 판정하지 않는다."""
    assert set(bits_of([0.5, -0.5] * 7 + [2.1, 0.0, 2.1] + [0.2, -0.2] * 8)) == {0}

def test_peek_does_not_change_state():
    s = NelsonState([0.5] * HISTORY)
    assert s.peek(0.5) & bit(2)
    assert s.peek(-0.5) & bit(2) == 0
    assert s.push(0.5) & bit(2)

def test_bounds_follow_rules():
    assert NelsonState().bounds() == (-3.0, 3.0)
    assert NelsonState([0.5] * 8).bounds() == (-3.0, 0.0)                  # 규칙 2: 반대쪽(0 포함)
    assert NelsonState([0.1, 0.2, 0.3, 0.4, 0.5]).bounds() == (-3.0, 0.5)  # 규칙 3: 직전 값 이하

def test_safe_avoids_dead_end():
    """8점 연속 음수이면서 마지막 4번 증가 중이면 다음 값이 없다. safe 는 그 상태로 가는 값을 거른다."""
    s = NelsonState([-0.5, -0.9, -0.8, -2.0, -1.5, -1.2, -0.9])
    assert not s.peek(-0.6)                      # 지금은 위반이 아니지만
    assert not s.safe(-0.6)                      # 넣으면 다음 값의 허용 구간이 빈다
    trapped = NelsonState([-0.5, -0.9, -0.8, -2.0, -1.5, -1.2, -0.9, -0.6])
    lo, hi = trapped.bounds()
    assert lo > hi
    assert s.safe(0.4)
