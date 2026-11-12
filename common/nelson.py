"""Nelson 8규칙 판정기. 시뮬레이터(표본 단위 NelsonState)와 수집기(배열 단위 judge)가 같은 규칙을 쓰도록 한 곳에 둔다.

z = (값 − 평균) / 시그마. 규칙 r 의 비트는 1 << (r - 1). 경계값은 "밖"이 아니며(엄격 부등호), 같은 값이
이어지면 추세(3)·교대(4)가 끊긴다. 규칙은 성립한 창의 마지막 표본에만 표시하고 앞쪽으로 되채우지 않는다.
두 구현은 같은 비트를 내야 한다(테스트로 고정).
"""
from __future__ import annotations

from collections import deque
from typing import Iterable

import numpy as np

HISTORY = 14        # 판정에 필요한 이전 표본 수 (규칙 7 의 15점 창 − 1)

# 반복해도 어떤 규칙도 성립하지 않는 z 패턴 (테스트용 공용 상수)
QUIET: tuple[float, ...] = (0.3, 0.6, -0.4, -1.2, 0.2, -0.7, 1.1, -0.3)


def bit(rule: int) -> int:
    """규칙 번호(1~8)의 비트."""
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
        """최근 최대 14개 z (수집기로 이어 붙일 꼬리)."""
        return list(self._buf)

    def peek(self, z: float) -> int:
        """z 를 넣었다고 칠 때의 비트. 상태는 바꾸지 않는다(기각 샘플링용)."""
        return self._bits(z)

    def push(self, z: float) -> int:
        """z 를 넣고 그 표본의 비트를 돌려준다."""
        b = self._bits(z)
        self._buf.append(z)
        return b

    def _bits(self, z: float) -> int:
        w = list(self._buf)[-HISTORY:] + [z]     # 마지막이 새 표본
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
        if n >= 14:
            s = [_sign(w[i] - w[i - 1]) for i in range(n - 13, n)]
            if all(s[i] * s[i - 1] < 0 for i in range(1, 13)):
                out |= bit(4)
        if n >= 3:
            last = w[-3:]
            if sum(x > 2 for x in last) >= 2 or sum(x < -2 for x in last) >= 2:
                out |= bit(5)
        if n >= 5:
            last = w[-5:]
            if sum(x > 1 for x in last) >= 4 or sum(x < -1 for x in last) >= 4:
                out |= bit(6)
        if n >= 15 and all(abs(x) < 1 for x in w[-15:]):
            out |= bit(7)
        if n >= 8:
            last = w[-8:]
            if all(abs(x) > 1 for x in last) and any(x > 0 for x in last) and any(x < 0 for x in last):
                out |= bit(8)
        return out


def _window(a: np.ndarray, n: int) -> np.ndarray:
    """a(0/1) 의 길이 n 이동 합. 창이 다 찬 위치(i ≥ n−1)만 값이 있고 나머지는 0."""
    out = np.zeros(len(a), dtype=np.int32)
    if len(a) >= n:
        c = np.concatenate(([0], np.cumsum(a, dtype=np.int32)))
        out[n - 1:] = c[n:] - c[:-n]
    return out


def judge(z: np.ndarray, tail: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """배열 판정. (hits, low) 를 uint8 로 돌려준다. low 는 규칙 1·2·3·5·6 이 아래쪽(감소)으로 성립한 비트.
    tail 은 직전 표본들(최대 14개 사용)이며 결과에는 포함되지 않는다."""
    z = np.asarray(z, dtype=float)
    k = 0
    if tail is not None and len(tail):
        t = np.asarray(tail, dtype=float)[-HISTORY:]
        k = len(t)
        z = np.concatenate((t, z))
    n = len(z)
    hits = np.zeros(n, dtype=np.uint8)
    low = np.zeros(n, dtype=np.uint8)

    def mark(mask: np.ndarray, rule: int, is_low: bool = False) -> None:
        hits[mask] |= bit(rule)
        if is_low:
            low[mask] |= bit(rule)

    pos, neg = z > 0, z < 0
    mark(z > 3, 1)
    mark(z < -3, 1, True)
    mark(_window(pos, 9) == 9, 2)
    mark(_window(neg, 9) == 9, 2, True)

    # i 에서 끝나는 차분의 부호 (첫 표본은 차분이 없어 거짓)
    d = np.diff(z)
    up, down = np.zeros(n, bool), np.zeros(n, bool)
    up[1:], down[1:] = d > 0, d < 0
    mark(_window(up, 5) == 5, 3)
    mark(_window(down, 5) == 5, 3, True)
    flip = np.zeros(n, bool)
    flip[2:] = (up[1:-1] & down[2:]) | (down[1:-1] & up[2:])
    mark(_window(flip, 12) == 12, 4)

    mark(_window(z > 2, 3) >= 2, 5)
    mark(_window(z < -2, 3) >= 2, 5, True)
    mark(_window(z > 1, 5) >= 4, 6)
    mark(_window(z < -1, 5) >= 4, 6, True)
    mark(_window(np.abs(z) < 1, 15) == 15, 7)
    outside = np.abs(z) > 1
    mark((_window(outside, 8) == 8) & (_window(pos, 8) >= 1) & (_window(neg, 8) >= 1), 8)

    return hits[k:], low[k:]
