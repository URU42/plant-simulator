"""펌프 1대와 병렬 토출 분기의 정상 유량 및 절대압 계산."""
from math import isclose, isfinite, sqrt
from typing import Annotated

from pydantic import Field, model_validator

from common.config_models import StrictModel
from simulator.physics.pipe import Pipe
from simulator.physics.pump import GRAVITY_M_S2, Nonnegative, Positive, Pump
from simulator.physics.valve import Valve

Opening = Annotated[float, Field(gt=0, le=1)]
VirtualID = Annotated[str, Field(pattern=r"^virtual:[A-Za-z0-9_-]+$")]


class Booster(StrictModel):
    pump: Pump
    speed_rpm: Positive
    discharge_pipe: Pipe


class Branch(StrictModel):
    branch_id: VirtualID
    outlet_node: str
    outlet_abs_pa: Positive
    elevation_change_m: float = Field(default=0.0, allow_inf_nan=False)
    pipes: dict[VirtualID, Pipe] = Field(min_length=1)
    valves: dict[VirtualID, Valve] = Field(default_factory=dict)
    booster: Booster | None = None


class OperatingPoint(StrictModel):
    suction_abs_pa: Positive
    speed_rpm: Positive
    valve_openings: dict[VirtualID, Opening] = Field(default_factory=dict)


class BranchResult(StrictModel):
    branch_id: str
    outlet_node: str
    outlet_abs_pa: float
    flow_m3_s: float
    friction_loss_pa: float
    elevation_pressure_pa: float
    pressure_residual_pa: float
    booster_discharge_abs_pa: float | None = None
    booster_shaft_w: float | None = None


class NetworkResult(StrictModel):
    suction_abs_pa: float
    discharge_abs_pa: float
    speed_rpm: float
    pump_flow_m3_s: float
    pump_head_m: float
    hydraulic_w: float
    shaft_w: float
    mass_residual_kg_s: float
    pump_pressure_residual_pa: float
    branches: list[BranchResult]


class Network(StrictModel):
    density_kg_m3: Positive
    pump: Pump
    branches: list[Branch] = Field(min_length=1)

    @model_validator(mode="after")
    def check_ids(self) -> "Network":
        ids = [key for branch in self.branches for key in (branch.branch_id, *branch.pipes, *branch.valves)]
        if len(set(ids)) != len(ids):
            raise ValueError("분기·배관·밸브 ID는 회로 안에서 중복될 수 없습니다.")
        return self

    def solve(self, point: OperatingPoint, suction_resistance: Nonnegative = 0.0) -> NetworkResult:
        density = self.density_kg_m3
        rho_g = density * GRAVITY_M_S2
        valves = {key for branch in self.branches for key in branch.valves}
        unknown = point.valve_openings.keys() - valves
        if unknown:
            raise ValueError(f"등록되지 않은 밸브: {', '.join(sorted(unknown))}")

        elevations = [rho_g * branch.elevation_change_m for branch in self.branches]
        boundaries = [branch.outlet_abs_pa + dz for branch, dz in zip(self.branches, elevations)]
        resistances = [
            sum(pipe.resistance(density) for pipe in branch.pipes.values())
            + sum(valve.resistance(density, point.valve_openings.get(key, 1.0))
                  for key, valve in branch.valves.items())
            for branch in self.branches
        ]
        boosters = [branch.booster for branch in self.branches]
        tail = [b.discharge_pipe.resistance(density) if b else 0.0 for b in boosters]
        supply_r = rho_g * self.pump.curvature + suction_resistance
        if not isfinite(suction_resistance) or suction_resistance < 0:
            raise ValueError("흡입측 저항은 유한한 음이 아닌 값이어야 합니다.")
        boundaries = [p - rho_g * b.pump.shutoff_at(b.speed_rpm) if b else p
                      for p, b in zip(boundaries, boosters)]
        total_r = [r + t + (rho_g * b.pump.curvature if b else 0.0)
                   for r, t, b in zip(resistances, tail, boosters)]
        source = point.suction_abs_pa
        shutoff_pressure = source + rho_g * self.pump.shutoff_at(point.speed_rpm)

        def flows_at(pressure_pa: float) -> tuple[float, list[float]]:
            # 역류 분기는 탐색 중 수요 0으로 두고, 해에서 역류가 필요한 회로는 거부한다.
            supply = sqrt(max(0.0, shutoff_pressure - pressure_pa) / supply_r)
            demands = [sqrt(max(0.0, pressure_pa - boundary) / resistance)
                       for boundary, resistance in zip(boundaries, total_r)]
            return supply, demands

        lo = max(source, min(boundaries))
        hi = shutoff_pressure
        if not isfinite(hi) or lo >= hi:
            raise ValueError("펌프 곡선과 배관 손실 곡선에 양의 유량 교점이 필요합니다.")
        supply, demands = flows_at(lo)
        if supply <= sum(demands):
            raise ValueError("펌프 양정이 양수인 운전점이 없습니다.")

        # 압력에 따라 공급은 감소하고 수요는 증가한다. 부동소수점으로 구별 가능한 구간까지 줄인다.
        for _ in range(80):
            mid = (lo + hi) / 2
            if mid == lo or mid == hi:
                break
            supply, demands = flows_at(mid)
            if supply > sum(demands):
                lo = mid
            else:
                hi = mid
        discharge = (lo + hi) / 2
        if discharge <= max(boundaries):
            raise ValueError("모든 분기에 양의 순방향 유량이 필요합니다. 역류·폐쇄 분기는 지원하지 않습니다.")
        supply, demands = flows_at(discharge)
        total_demand = sum(demands)
        if not isclose(supply, total_demand, rel_tol=1e-9, abs_tol=1e-12):
            raise ValueError("요청한 운전점의 유량 수지가 수치 허용 오차 내에서 수렴하지 않았습니다.")
        head = self.pump.head_m(supply, point.speed_rpm)
        hydraulic = supply * rho_g * head
        suction = source - suction_resistance * supply**2
        if suction <= 0:
            raise ValueError("흡입측 저항 적용 후 펌프 흡입 절대압이 양수여야 합니다.")
        results = []
        for branch, q, r, dz, booster, tail_r in zip(
            self.branches, demands, resistances, elevations, boosters, tail,
        ):
            loss = (r + tail_r) * q**2
            boost = rho_g * booster.pump.head_m(q, booster.speed_rpm) if booster else 0.0
            if booster and discharge - dz - r * q**2 <= 0:
                raise ValueError("후속 펌프 흡입 절대압이 양수여야 합니다.")
            results.append(BranchResult(
                branch_id=branch.branch_id, outlet_node=branch.outlet_node,
                outlet_abs_pa=branch.outlet_abs_pa, flow_m3_s=q, friction_loss_pa=loss,
                elevation_pressure_pa=dz,
                pressure_residual_pa=discharge + boost - branch.outlet_abs_pa - dz - loss,
                booster_discharge_abs_pa=branch.outlet_abs_pa + tail_r * q**2 if booster else None,
                booster_shaft_w=q * boost / booster.pump.efficiency if booster else None,
            ))
        return NetworkResult(suction_abs_pa=suction, discharge_abs_pa=discharge, speed_rpm=point.speed_rpm,
                             pump_flow_m3_s=supply, pump_head_m=head, hydraulic_w=hydraulic,
                             shaft_w=hydraulic / self.pump.efficiency,
                             mass_residual_kg_s=density * (supply - total_demand),
                             pump_pressure_residual_pa=discharge - suction - rho_g * head, branches=results)
