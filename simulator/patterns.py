"""이상 패턴 모음 (기획서 5.3). 시나리오 파일에서 이름으로 골라 쓰는 재사용 부품.

값 패턴(ramp / step / spike / oscillation)은 진행률(0~1)에 따른 편차를 돌려준다.
freeze / quality / gap 은 값을 더하는 게 아니라 샘플 자체를 바꾸므로 scenario.py 가 따로 처리한다.
"""
from __future__ import annotations

from typing import Callable

import numpy as np

Shape = Callable[[np.ndarray, np.ndarray, float], np.ndarray]   # (progress, epochs, magnitude) → 편차

SPIKE_DECAY = 5.0               # spike: magnitude × exp(-5 × 진행률)
OSCILLATION_PERIOD_SEC = 120    # oscillation 주기


def ramp(progress: np.ndarray, epochs: np.ndarray, magnitude: float) -> np.ndarray:
    """점진 상승·하강 (베어링 마모, 필터 막힘)."""
    return magnitude * progress


def step(progress: np.ndarray, epochs: np.ndarray, magnitude: float) -> np.ndarray:
    """계단형 변화 (운전 조건 변경, 부품 이탈)."""
    return np.full_like(progress, magnitude, dtype=float)


def spike(progress: np.ndarray, epochs: np.ndarray, magnitude: float) -> np.ndarray:
    """순간 튐 뒤 감쇠 (일시적 충격, 계기 잡음)."""
    return magnitude * np.exp(-SPIKE_DECAY * progress)


def oscillation(progress: np.ndarray, epochs: np.ndarray, magnitude: float) -> np.ndarray:
    """진동 폭이 점점 커지는 흔들림 (정렬 불량, 서지)."""
    return magnitude * progress * np.sin(2 * np.pi * epochs / OSCILLATION_PERIOD_SEC)


VALUE_PATTERNS = {"ramp", "step", "spike", "oscillation"}
SAMPLE_PATTERNS = {"freeze", "quality", "gap"}      # 값 멈춤 / Scan Off·Bad / 데이터 끊김
PATTERNS: dict[str, Shape | None] = {"ramp": ramp, "step": step, "spike": spike, "oscillation": oscillation,
                                      "freeze": None, "quality": None, "gap": None}


def shape(pattern: str, progress: np.ndarray, epochs: np.ndarray, magnitude: float) -> np.ndarray:
    fn = PATTERNS[pattern]
    if fn is None:
        raise ValueError(f"{pattern} is not a value pattern")
    return fn(progress, epochs, magnitude)
