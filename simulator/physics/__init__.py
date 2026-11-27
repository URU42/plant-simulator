"""기존 재생 데모용 단일 분기 수력·에너지 어댑터."""
from dataclasses import dataclass

from pydantic import Field

from common.config_models import StrictModel
from simulator.physics.network import Branch, Network, OperatingPoint
from simulator.physics.pipe import Pipe
from simulator.physics.pump import GRAVITY_M_S2, Pump

KGF_CM2_PA = 98_066.5


class PumpCircuit(StrictModel):
    plant_id: str
    unit_id: str
    equip_id: str
    outlet_node: str
    provenance: str
    atmospheric_pa: float = Field(gt=0, allow_inf_nan=False)
    density_kg_m3: float = Field(gt=0, allow_inf_nan=False)
    heat_capacity_j_kg_k: float = Field(gt=0, allow_inf_nan=False)
    inlet_temp_k: float = Field(gt=0, allow_inf_nan=False)
    nominal_flow_m3_s: float = Field(gt=0, allow_inf_nan=False)
    shutoff_rise_pa: float = Field(gt=0, allow_inf_nan=False)
    nominal_rise_pa: float = Field(gt=0, allow_inf_nan=False)
    efficiency: float = Field(gt=0, le=1)
    pipe_length_m: float = Field(gt=0, allow_inf_nan=False)
    pipe_diameter_m: float = Field(gt=0, allow_inf_nan=False)
    darcy_factor: float = Field(gt=0, allow_inf_nan=False)
    minor_loss_coefficient: float = Field(ge=0, allow_inf_nan=False)
    outlet_abs_pa: float = Field(gt=0, allow_inf_nan=False)

    def solve(self, suction_abs_pa: float) -> "PumpState":
        if not 0 < suction_abs_pa <= self.outlet_abs_pa:
            raise ValueError("흡입 절대압은 양수이며 토출 배관 경계압 이하여야 합니다.")
        available_pa = suction_abs_pa + self.shutoff_rise_pa - self.outlet_abs_pa
        if not 0 < self.nominal_rise_pa < self.shutoff_rise_pa or available_pa <= 0:
            raise ValueError("펌프 곡선과 배관 손실 곡선에 양의 유량 교점이 필요합니다.")

        rho_g = self.density_kg_m3 * GRAVITY_M_S2
        network = Network(
            density_kg_m3=self.density_kg_m3,
            pump=Pump(reference_speed_rpm=1, rated_flow_m3_s=self.nominal_flow_m3_s,
                      shutoff_head_m=self.shutoff_rise_pa / rho_g, rated_head_m=self.nominal_rise_pa / rho_g,
                      efficiency=self.efficiency),
            branches=[Branch(branch_id="virtual:replay-discharge", outlet_node=self.outlet_node,
                             outlet_abs_pa=self.outlet_abs_pa,
                             pipes={"virtual:replay-pipe": Pipe(length_m=self.pipe_length_m,
                                    diameter_m=self.pipe_diameter_m, darcy_factor=self.darcy_factor,
                                    minor_loss_k=self.minor_loss_coefficient)})],
        )
        result = network.solve(OperatingPoint(suction_abs_pa=suction_abs_pa, speed_rpm=1))
        branch = result.branches[0]
        flow = result.pump_flow_m3_s
        discharge_abs_pa = result.discharge_abs_pa
        pump_flow = self.density_kg_m3 * flow
        outlet_flow = self.density_kg_m3 * branch.flow_m3_s
        hydraulic_w, shaft_w = result.hydraulic_w, result.shaft_w
        temp_rise_k = (shaft_w - hydraulic_w + branch.flow_m3_s * branch.friction_loss_pa) / (
            pump_flow * self.heat_capacity_j_kg_k)
        outlet_temp_k = self.inlet_temp_k + temp_rise_k
        energy_out_minus_in_w = (
            flow * (self.outlet_abs_pa - suction_abs_pa)
            + pump_flow * self.heat_capacity_j_kg_k * (outlet_temp_k - self.inlet_temp_k)
        )
        return PumpState(flow, suction_abs_pa, discharge_abs_pa, self.outlet_abs_pa, self.inlet_temp_k,
                         outlet_temp_k, shaft_w, hydraulic_w, pump_flow, outlet_flow,
                         pump_flow - outlet_flow, shaft_w - energy_out_minus_in_w)


@dataclass(frozen=True)
class PumpState:
    flow_m3_s: float
    suction_abs_pa: float
    discharge_abs_pa: float
    outlet_abs_pa: float
    inlet_temp_k: float
    outlet_temp_k: float
    shaft_w: float
    hydraulic_w: float
    inlet_mass_kg_s: float
    outlet_mass_kg_s: float
    mass_residual_kg_s: float
    energy_residual_w: float
