"""개도에 따른 국부손실 밸브 모델."""
from math import isfinite, pi, sqrt

from common.config_models import StrictModel
from simulator.physics.pump import Positive, require_flow, require_positive

# 1 US gpm @ 1 psi → m³/h @ 1 bar
CV_TO_KV = 3.785411784e-3 * 60 * sqrt(1e5 / 6894.757293168)


class Valve(StrictModel):
    diameter_m: Positive
    full_open_k: Positive

    def resistance(self, density_kg_m3: float, opening: float) -> float:
        require_positive(density_kg_m3, "밀도")
        if not isfinite(opening) or not 0 < opening <= 1:
            raise ValueError("밸브 개도는 0 초과 1 이하여야 합니다.")
        area_m2 = pi * self.diameter_m**2 / 4
        return density_kg_m3 * self.full_open_k / (2 * (area_m2 * opening)**2)

    def pressure_loss_pa(self, flow_m3_s: float, density_kg_m3: float, opening: float) -> float:
        require_flow(flow_m3_s)
        return self.resistance(density_kg_m3, opening) * flow_m3_s**2

    def kv(self, opening: float) -> float:
        """Kv [m³/h, 물 1000 kg/m³, Δp 1 bar]. K(a) = full_open_k / a² 와 같은 식의 다른 표현이다."""
        return 3600 * sqrt(1e5 / self.resistance(1000.0, opening))

    def cv(self, opening: float) -> float:
        """Cv [US gpm, 비중 1, Δp 1 psi]."""
        return self.kv(opening) / CV_TO_KV


class CheckValve(StrictModel):
    """역류를 막는 체크밸브. 순방향은 고정 손실계수이고 역방향은 닫힘(누설 없음). 개폐 과도는 계산하지 않는다."""
    diameter_m: Positive
    forward_k: Positive

    def resistance(self, density_kg_m3: float) -> float:
        require_positive(density_kg_m3, "밀도")
        area_m2 = pi * self.diameter_m**2 / 4
        return density_kg_m3 * self.forward_k / (2 * area_m2**2)
