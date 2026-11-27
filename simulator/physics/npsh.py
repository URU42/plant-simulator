"""펌프 NPSH·캐비테이션 양정 감소 (합성 모델, D0 P-8303A → D2-B 결합 회로 공용).

- 증기압: Clausius–Clapeyron 형 pv(T) = pv0 · exp(B · (1/T0 − 1/T)).
- NPSHa = (p_inlet − pv)/(ρg) + v²/(2g), NPSHr(Q, N) = (N/N0)² · NPSHr_rated · (c0 + (1 − c0)(Q/(N/N0 · Q_rated))²).
- 여유비 s = NPSHa/NPSHr. 양정 감소 d(s): s ≥ s0 → 0, 1 ≤ s < s0 → 0.03x², s < 1 → 0.03 + 0.06(x−1) + k(x−1)², 상한 d_max.
  (x = (s0 − s)/(s0 − 1). NPSHr 정의 3 % 양정 저하와 맞춘 값·기울기 연속 곡선.) 효율 = η(1 − e·d).
모든 계수는 synthetic assumption 이다. 기포 동역학·침식·진동·과도응답은 계산하지 않는다.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from math import exp, pi, sqrt
from typing import Annotated

from pydantic import Field

from common.config_models import StrictModel
from simulator.physics.pump import GRAVITY_M_S2, Positive

Fraction = Annotated[float, Field(gt=0, lt=1)]

# 캐비테이션 붕괴 외삽 (M2 시뮬레이션 정적 계산 전용, 2026-11-26 사용자 승인).
# 켜져 있으면 양정 감소가 상한 d_max 에 닿는 여유비 s_cap 아래에서 멈추지 않고 s = 0 에서 1(양정 없음)까지 선형으로 이어진다.
# 유량이 늘수록 여유비가 줄어 양정이 무너지므로 운전점은 NPSH 로 제한된 유량에서 정해진다(검증하지 않은 합성 외삽).
# 수집기 트윈·보정 스크립트는 켜지 않는다(기본 꺼짐). simulator.static_calc 가 계산하는 동안에만 켠다.
_COLLAPSE: ContextVar[bool] = ContextVar("npsh_collapse", default=False)


@contextmanager
def collapse_extrapolation():
    token = _COLLAPSE.set(True)
    try:
        yield
    finally:
        _COLLAPSE.reset(token)


def collapse_enabled() -> bool:
    return _COLLAPSE.get()


class Npsh(StrictModel):
    fluid_temp_k: Positive                 # 정상 흡입 유체 온도 (마스터 흡입 유체 온도 태그의 μ)
    vapor_pressure_pa: Positive            # 그 온도의 증기압 (synthetic)
    vapor_b_k: Positive                    # Clausius–Clapeyron 계수 B (synthetic)
    suction_diameter_m: Positive
    npshr_rated_m: Positive
    npshr_zero_flow_fraction: Fraction
    onset_margin: float = Field(gt=1, allow_inf_nan=False)       # s0
    steep_coefficient: Positive            # s < 1 구간 k
    max_head_loss: Fraction                # d_max
    efficiency_coupling: float = Field(ge=0, le=1)               # e

    def vapor_pressure(self, temp_k: float) -> float:
        return self.vapor_pressure_pa * exp(self.vapor_b_k * (1 / self.fluid_temp_k - 1 / temp_k))

    def head_loss(self, margin: float) -> float:
        if margin >= self.onset_margin:
            return 0.0
        if _COLLAPSE.get() and margin < self.collapse_margin:
            return self.max_head_loss + (1 - self.max_head_loss) * (1 - max(margin, 0.0) / self.collapse_margin)
        x = (self.onset_margin - margin) / (self.onset_margin - 1)
        d = 0.03 * x**2 if x <= 1 else 0.03 + 0.06 * (x - 1) + self.steep_coefficient * (x - 1)**2
        return min(d, self.max_head_loss)

    @property
    def collapse_margin(self) -> float:
        """양정 감소가 d_max 에 닿는 여유비 s_cap (s < 1 구간 2차식의 근)."""
        k, dmax = self.steep_coefficient, self.max_head_loss
        if dmax <= 0.03:
            x = sqrt(dmax / 0.03)
        else:
            x = 1 + (-0.06 + sqrt(0.0036 + 4 * k * (dmax - 0.03))) / (2 * k)
        return self.onset_margin - x * (self.onset_margin - 1)



    def evaluate(self, flow_m3_s: float, inlet_abs_pa: float, density: float, speed_ratio: float,
                 rated_flow_m3_s: float, temp_k: float | None = None) -> tuple[float, float, float, float, float]:
        """(증기압, NPSHa, NPSHr, 여유비, 양정 감소율)."""
        pv = self.vapor_pressure(self.fluid_temp_k if temp_k is None else temp_k)
        v = flow_m3_s / (pi * self.suction_diameter_m**2 / 4)
        npsha = (inlet_abs_pa - pv) / (density * GRAVITY_M_S2) + v**2 / (2 * GRAVITY_M_S2)
        npshr = speed_ratio**2 * self.npshr_rated_m * (self.npshr_zero_flow_fraction + (1 - self.npshr_zero_flow_fraction)
                                                       * (flow_m3_s / (speed_ratio * rated_flow_m3_s))**2)
        margin = npsha / npshr
        return pv, npsha, npshr, margin, self.head_loss(margin)
