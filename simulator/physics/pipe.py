"""고정 Darcy 마찰계수를 사용하는 원형 배관."""
from math import pi

from common.config_models import StrictModel
from simulator.physics.pump import Nonnegative, Positive, require_flow, require_positive


class Pipe(StrictModel):
    length_m: Positive
    diameter_m: Positive
    darcy_factor: Positive
    minor_loss_k: Nonnegative = 0.0

    def resistance(self, density_kg_m3: float) -> float:
        require_positive(density_kg_m3, "밀도")
        area_m2 = pi * self.diameter_m**2 / 4
        return density_kg_m3 * (self.darcy_factor * self.length_m / self.diameter_m + self.minor_loss_k) / (2 * area_m2**2)

    def pressure_loss_pa(self, flow_m3_s: float, density_kg_m3: float) -> float:
        require_flow(flow_m3_s)
        return self.resistance(density_kg_m3) * flow_m3_s**2
