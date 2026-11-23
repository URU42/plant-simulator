"""개도에 따른 국부손실 밸브 모델."""
from math import isfinite, pi

from common.config_models import StrictModel
from simulator.physics.pump import Positive, require_flow, require_positive


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
