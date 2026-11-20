"""Nelson 규칙 1·2·3 판정기. 생성기가 정상 신호를 고를 때(억제 표집) 쓴다. 탐지 쪽도 이 세 규칙만 쓴다.

z = (값 − 평균) / 시그마. 규칙 r 의 비트는 1 << (r - 1). 경계값은 "밖"이 아니며(엄격 부등호), 같은 값이
이어지면 추세(3)가 끊긴다.
- 규칙 1: 한 점이 ±3σ 밖
- 규칙 2: 9점 연속 중심선의 같은 쪽
- 규칙 3: 6점 연속 엄격히 증가 또는 감소
"""
from __future__ import annotations

from collections import deque
from typing import Iterable

HISTORY = 8         # 판정에 필요한 이전 표본 수 (규칙 2 의 9점 창 − 1)

# 반복해도 어떤 규칙도 성립하지 않는 z 패턴 (테스트용 공용 상수)
QUIET: tuple[float, ...] = (0.3, 0.6, -0.4, -1.2, 0.2, -0.7, 1.1, -0.3)


def bit(rule: int) -> int:
    """규칙 번호(1~3)의 비트."""
    return 1 << (rule - 1)


def zscore(value: float, mean: float, sigma: float) -> float:
    return (value - mean) / sigma


def _sign(x: float) -> int:
    return int(x > 0) - int(x < 0)


class NelsonState:
    """표본 단위 판정. 최근 z 만 들고 규칙마다 고정 길이 창만 다시 보므로 표본당 상수 시간이다."""

    def __init__(self, history: Iterable[float] = ()):
        self._buf: deque[float] = deque(list(history)[-HISTORY:], maxlen=HISTORY)

    def tail(self) -> list[float]:
        """최근 최대 HISTORY 개 z."""
        return list(self._buf)

    def peek(self, z: float) -> int:
        """z 를 넣었다고 칠 때의 비트. 상태는 바꾸지 않는다(기각 샘플링용)."""
        return self._bits(z)

    def push(self, z: float) -> int:
        """z 를 넣고 그 표본의 비트를 돌려준다."""
        b = self._bits(z)
        self._buf.append(z)
        return b

    def bounds(self) -> tuple[float, float]:
        """다음 z 가 어떤 규칙도 어기지 않는 닫힌 구간 [lo, hi]. lo > hi 면 그런 값이 없다."""
        return _bounds(list(self._buf))

    def safe(self, z: float) -> bool:
        """z 가 규칙을 어기지 않고, 넣은 뒤에도 규칙을 어기지 않는 다음 값이 남는가 (억제 표집이 막다른 곳에 빠지지 않게).
        예: 8점 연속 음수이면서 마지막 4번이 증가 중이면 다음 값은 0 이상(규칙 2)이면서 직전 값 이하(규칙 3)여야 해 불가능하다."""
        if self._bits(z):
            return False
        lo, hi = _bounds((list(self._buf) + [z])[-HISTORY:])
        return lo <= hi

    def _bits(self, z: float) -> int:
        w = list(self._buf) + [z]     # 마지막이 새 표본
        n = len(w)
        out = 0
        if abs(z) > 3:
            out |= bit(1)
        if n >= 9 and (all(x > 0 for x in w[-9:]) or all(x < 0 for x in w[-9:])):
            out |= bit(2)
        if n >= 6:
            s = [_sign(w[i] - w[i - 1]) for i in range(n - 5, n)]
            if all(x > 0 for x in s) or all(x < 0 for x in s):
                out |= bit(3)
        return out


def _bounds(w: list[float]) -> tuple[float, float]:
    """이력 w(오래된 것부터) 다음에 올 z 의 허용 구간. 규칙 1: |z| ≤ 3, 규칙 2: 8점이 모두 한쪽이면 반대쪽(0 포함),
    규칙 3: 마지막 5점이 엄격히 증가(감소)면 직전 값 이하(이상)."""
    lo, hi = -3.0, 3.0
    if len(w) >= 8:
        if all(x > 0 for x in w[-8:]):
            hi = min(hi, 0.0)
        elif all(x < 0 for x in w[-8:]):
            lo = max(lo, 0.0)
    if len(w) >= 5:
        s = [_sign(w[i] - w[i - 1]) for i in range(len(w) - 4, len(w))]
        if all(x > 0 for x in s):
            hi = min(hi, w[-1])
        elif all(x < 0 for x in s):
            lo = max(lo, w[-1])
    return lo, hi
