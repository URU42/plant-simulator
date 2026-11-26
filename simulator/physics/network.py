"""펌프 1대와 병렬 토출 분기의 정상 유량 및 절대압 계산."""
from math import isclose, isfinite, sqrt
from typing import Annotated, Literal

from pydantic import Field, model_validator

from common.config_models import StrictModel
from simulator.physics.npsh import Npsh, collapse_enabled
from simulator.physics.pipe import Pipe
from simulator.physics.pump import GRAVITY_M_S2, Nonnegative, Positive, Pump
from simulator.physics.valve import Valve

Opening = Annotated[float, Field(gt=0, le=1)]
VirtualID = Annotated[str, Field(pattern=r"^virtual:[A-Za-z0-9_-]+$")]


def root(fn, hi: float) -> float:
    """fn 단조 감소, fn(0) > 0 이면 fn(q)=0 인 q, 아니면 0."""
    lo = 0.0
    if fn(lo) <= 0:
        return 0.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if mid in (lo, hi):
            break
        if fn(mid) > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


class Booster(StrictModel):
    pump: Pump
    speed_rpm: Positive
    discharge_pipe: Pipe
    npsh: Npsh | None = None          # 후속 펌프 NPSH·캐비테이션 (D2-B, synthetic)


class Drum(StrictModel):
    """후속 펌프 흡입 드럼(탑 하부·환류·재순환 드럼, synthetic assumption). 정상 상태 대용모델.

    분기를 드럼에서 끊는다. 유입: 헤더 → 분기 배관·밸브 → 드럼(끝 압력 inlet_abs_pa). 유출: 드럼 → 후속 펌프 흡입
    (inlet_abs_pa − 후속 펌프 흡입 저항·Q²) → 후속 펌프 → 토출 배관 중 (1 − share) + 액위 조절 밸브 → 출구 경계.
    액위 조절 밸브가 유출 = 유입을 맞춘다(액위는 설정값에 머문다). 밸브가 끝까지 열리거나 닫혀도 못 맞추면 정상 상태가
    없다(액위 상승·하강, state 로 알린다). inlet_abs_pa 는 설정 액위에서 흡입 노즐 압력 = 정상 후속 펌프 흡입압이라
    정상 운전점은 드럼 없는 등가회로와 같다. 밸브 몫·정상 개도는 CDU 시간 재생(D6) 액위 제어밸브와 같은 합성 값이다.
    """
    inlet_abs_pa: Positive
    level_valve_share: float = Field(default=0.3, gt=0, lt=1)
    normal_opening: float = Field(default=0.5, gt=0, le=1)
    min_opening: float = Field(default=0.05, gt=0, le=1)

    @model_validator(mode="after")
    def check_opening(self) -> "Drum":
        if self.min_opening > self.normal_opening:
            raise ValueError("액위 조절 밸브 최소 개도는 정상 개도 이하여야 합니다.")
        return self


class Branch(StrictModel):
    branch_id: VirtualID
    outlet_node: str
    outlet_abs_pa: Positive
    elevation_change_m: float = Field(default=0.0, allow_inf_nan=False)
    pipes: dict[VirtualID, Pipe] = Field(min_length=1)
    valves: dict[VirtualID, Valve] = Field(default_factory=dict)
    booster: Booster | None = None
    drum: Drum | None = None

    @model_validator(mode="after")
    def check_drum(self) -> "Branch":
        if self.drum is not None and self.booster is None:
            raise ValueError("드럼은 후속 펌프가 있는 분기에만 둘 수 있습니다.")
        return self


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
    booster_suction_abs_pa: float | None = None
    booster_discharge_abs_pa: float | None = None
    booster_shaft_w: float | None = None
    booster_hydraulic_w: float | None = None
    booster_npsha_m: float | None = None
    booster_npshr_m: float | None = None
    booster_npsh_margin: float | None = None
    booster_head_loss: float | None = None
    booster_flow_m3_s: float | None = None        # 드럼 분기: 후속 펌프 유량(유출). flow_m3_s 는 드럼 유입
    booster_strainer_loss_pa: float | None = None # 후속 펌프 앞 스트레이너 손실 (2026-11-27, 스트레이너 차압 계측용)
    level_valve_opening: float | None = None      # 드럼 분기: 액위 조절 밸브 개도
    drum_state: Literal["steady", "level_rising", "level_falling"] | None = None


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
    pump_npsha_m: float | None = None
    pump_npshr_m: float | None = None
    pump_npsh_margin: float | None = None
    pump_head_loss: float | None = None


class Network(StrictModel):
    density_kg_m3: Positive
    pump: Pump
    branches: list[Branch] = Field(min_length=1)
    pump_npsh: Npsh | None = None     # 입력 펌프 NPSH·캐비테이션 (D2-B, synthetic)

    @model_validator(mode="after")
    def check_ids(self) -> "Network":
        ids = [key for branch in self.branches for key in (branch.branch_id, *branch.pipes, *branch.valves)]
        if len(set(ids)) != len(ids):
            raise ValueError("분기·배관·밸브 ID는 회로 안에서 중복될 수 없습니다.")
        return self

    def solve(self, point: OperatingPoint, suction_resistance: Nonnegative = 0.0,
              booster_suction_resistance: Nonnegative = 0.0) -> NetworkResult:
        """suction_resistance 는 주 펌프 상류, booster_suction_resistance 는 후속 펌프가 있는 분기에서 후속 펌프 바로 앞의
        국부 저항(Pa·s²/m⁶)이다. 둘 다 Δp = R·Q² 형태다.
        NPSH 설정이 있으면 캐비테이션 양정 감소를 넣은 일반 해법(_solve_npsh)을 쓴다. 없으면 기존 닫힌 형식 그대로다."""
        if self.pump_npsh is not None or any((b.booster and b.booster.npsh) or b.drum for b in self.branches):
            return self._solve_npsh(point, suction_resistance, booster_suction_resistance)
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
        if not isfinite(booster_suction_resistance) or booster_suction_resistance < 0:
            raise ValueError("후속 펌프 흡입측 저항은 유한한 음이 아닌 값이어야 합니다.")
        if booster_suction_resistance and not any(boosters):
            raise ValueError("후속 펌프가 없는 회로에는 후속 펌프 흡입측 저항을 줄 수 없습니다.")
        # 후속 펌프 흡입 저항은 그 분기의 직렬 저항에 더한다.
        resistances = [r + (booster_suction_resistance if b else 0.0) for r, b in zip(resistances, boosters)]
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
            booster_suction = discharge - dz - r * q**2 if booster else None
            if booster_suction is not None and booster_suction <= 0:
                raise ValueError("후속 펌프 흡입 절대압이 양수여야 합니다.")
            results.append(BranchResult(
                branch_id=branch.branch_id, outlet_node=branch.outlet_node,
                outlet_abs_pa=branch.outlet_abs_pa, flow_m3_s=q, friction_loss_pa=loss,
                elevation_pressure_pa=dz,
                pressure_residual_pa=discharge + boost - branch.outlet_abs_pa - dz - loss,
                booster_suction_abs_pa=booster_suction,
                booster_discharge_abs_pa=branch.outlet_abs_pa + tail_r * q**2 if booster else None,
                booster_shaft_w=q * boost / booster.pump.efficiency if booster else None,
                booster_hydraulic_w=q * boost if booster else None,
            ))
        return NetworkResult(suction_abs_pa=suction, discharge_abs_pa=discharge, speed_rpm=point.speed_rpm,
                             pump_flow_m3_s=supply, pump_head_m=head, hydraulic_w=hydraulic,
                             shaft_w=hydraulic / self.pump.efficiency,
                             mass_residual_kg_s=density * (supply - total_demand),
                             pump_pressure_residual_pa=discharge - suction - rho_g * head, branches=results)

    # --- NPSH·캐비테이션 포함 해법 ----------------------------------------------------------
    def _solve_npsh(self, point: OperatingPoint, suction_resistance: float, booster_suction_resistance: float) -> NetworkResult:
        """헤더 압력 이분법 안에서 입력 펌프 공급 유량과 분기별 수요 유량을 각각 단조 방정식의 이분법으로 푼다.
        양정 = H(Q)·(1 − d(s)), s 는 그 유량의 펌프 입구 절대압에서 계산한 NPSHa/NPSHr."""
        density = self.density_kg_m3
        rho_g = density * GRAVITY_M_S2
        if not isfinite(suction_resistance) or suction_resistance < 0:
            raise ValueError("흡입측 저항은 유한한 음이 아닌 값이어야 합니다.")
        source, speed = point.suction_abs_pa, point.speed_rpm
        ratio = speed / self.pump.reference_speed_rpm
        npsh = self.pump_npsh

        def feed(q: float):
            p_in = source - suction_resistance * q**2
            loss = 0.0
            if npsh is not None:
                loss = npsh.evaluate(q, p_in, density, ratio, self.pump.rated_flow_m3_s)[4]
            head = self.pump.shutoff_at(speed) - self.pump.curvature * q**2
            return p_in, head * (1 - loss), loss

        q_feed_max = sqrt(self.pump.shutoff_at(speed) / self.pump.curvature)

        def supply(p: float) -> float:
            return root(lambda q: feed(q)[0] + rho_g * feed(q)[1] - p, q_feed_max)

        side = DemandSide(self, point, booster_suction_resistance)
        demand = side.demand

        hi = source + rho_g * self.pump.shutoff_at(speed)
        lo = 1.0
        if supply(lo) <= sum(demand(i, lo) for i in range(len(self.branches))):
            raise ValueError("펌프 양정이 양수인 운전점이 없습니다.")
        for _ in range(200):
            mid = (lo + hi) / 2
            if mid in (lo, hi):
                break
            if supply(mid) > sum(demand(i, mid) for i in range(len(self.branches))):
                lo = mid
            else:
                hi = mid
        discharge = (lo + hi) / 2
        q = supply(discharge)
        flows = [demand(i, discharge) for i in range(len(self.branches))]
        if q <= 0 or any(f <= 0 for f in flows):
            raise ValueError("모든 분기에 양의 순방향 유량이 필요합니다. 역류·폐쇄 분기는 지원하지 않습니다.")
        if not isclose(q, sum(flows), rel_tol=1e-9, abs_tol=1e-12):
            raise ValueError("요청한 운전점의 유량 수지가 수치 허용 오차 내에서 수렴하지 않았습니다.")
        suction, head, loss = feed(q)
        if suction <= 0:
            raise ValueError("흡입측 저항 적용 후 펌프 흡입 절대압이 양수여야 합니다.")
        fields = {}
        if npsh is not None:
            _, npsha, npshr, margin, loss = npsh.evaluate(q, suction, density, ratio, self.pump.rated_flow_m3_s)
            if npsha <= 0 or (loss >= npsh.max_head_loss and not collapse_enabled()):
                raise ValueError(f"캐비테이션 모델 유효 범위 밖: 입력 펌프 NPSHa {npsha:.2f} m, 양정 감소 {loss:.2f}")
            fields = {"pump_npsha_m": npsha, "pump_npshr_m": npshr, "pump_npsh_margin": margin, "pump_head_loss": loss}
        eta = self.pump.efficiency * (1 - (npsh.efficiency_coupling if npsh else 0.0) * loss)
        hydraulic = q * rho_g * head
        return NetworkResult(suction_abs_pa=suction, discharge_abs_pa=discharge, speed_rpm=speed, pump_flow_m3_s=q,
                             pump_head_m=head, hydraulic_w=hydraulic, shaft_w=hydraulic / eta,
                             mass_residual_kg_s=density * (q - sum(flows)),
                             pump_pressure_residual_pa=discharge - suction - rho_g * head, branches=side.results(discharge, flows), **fields)


class DemandSide:
    """토출 헤더 하류(분기·밸브·후속 펌프). 헤더 절대압 → 분기별 순방향 유량과 결과. 펌프 1대와 펌프 묶음(station)이 같이 쓴다."""

    def __init__(self, network: Network, point: OperatingPoint, booster_suction_resistance: float) -> None:
        self.network, self.density = network, network.density_kg_m3
        self.rho_g = self.density * GRAVITY_M_S2
        unknown = point.valve_openings.keys() - {key for branch in network.branches for key in branch.valves}
        if unknown:
            raise ValueError(f"등록되지 않은 밸브: {', '.join(sorted(unknown))}")
        if not isfinite(booster_suction_resistance) or booster_suction_resistance < 0:
            raise ValueError("흡입측 저항은 유한한 음이 아닌 값이어야 합니다.")
        self.r, self.tails, self.elevations = [], [], []
        self.booster_r = booster_suction_resistance
        for branch in network.branches:
            r = sum(pipe.resistance(self.density) for pipe in branch.pipes.values()) \
                + sum(v.resistance(self.density, point.valve_openings.get(k, 1.0)) for k, v in branch.valves.items())
            # 드럼 분기의 후속 펌프 흡입 저항은 드럼 → 후속 펌프 쪽(유출)에 둔다.
            self.r.append(r + (booster_suction_resistance if branch.booster and not branch.drum else 0.0))
            self.tails.append(branch.booster.discharge_pipe.resistance(self.density) if branch.booster else 0.0)
            self.elevations.append(self.rho_g * branch.elevation_change_m)

    def boost(self, branch: Branch, q: float, p_suction: float) -> tuple[float, float]:
        b = branch.booster
        if b is None:
            return 0.0, 0.0
        loss = b.npsh.evaluate(q, p_suction, self.density, b.speed_rpm / b.pump.reference_speed_rpm,
                               b.pump.rated_flow_m3_s)[4] if b.npsh else 0.0
        head = b.pump.shutoff_at(b.speed_rpm) - b.pump.curvature * q**2
        return self.rho_g * head * (1 - loss), loss

    def demand(self, i: int, p: float) -> float:
        branch, r, tail, dz = self.network.branches[i], self.r[i], self.tails[i], self.elevations[i]
        if branch.drum is not None:          # 드럼 유입: 헤더 → 드럼 끝 압력
            return sqrt(max(0.0, p - dz - branch.drum.inlet_abs_pa) / max(r, 1e-30))
        b = branch.booster
        q_max = sqrt(max(p, 1.0) / max(r, 1e-30)) * 2 + (sqrt(b.pump.shutoff_at(b.speed_rpm) / b.pump.curvature) if b else 0)
        return root(lambda q: p - dz - r * q**2 + self.boost(branch, q, p - dz - r * q**2)[0] - tail * q**2 - branch.outlet_abs_pa,
                    q_max)

    def drum_out(self, i: int, q_in: float) -> dict:
        """드럼 분기 유출(정상 상태). 액위 조절 밸브로 유출 = 유입을 맞추고, 못 맞추면 끝 개도의 유출과 상태를 낸다."""
        branch, tail = self.network.branches[i], self.tails[i]
        d, b = branch.drum, branch.booster
        r_valve0 = d.level_valve_share * tail                 # 정상 개도에서 밸브 저항(정상점 총 토출 저항 유지)
        r_pipe = tail - r_valve0

        def suction(q: float) -> float:
            return d.inlet_abs_pa - self.booster_r * q * q

        def excess(q: float, opening: float) -> float:
            p = suction(q)
            r_valve = r_valve0 * (d.normal_opening / opening) ** 2
            return p + self.boost(branch, q, p)[0] - (r_pipe + r_valve) * q * q - branch.outlet_abs_pa

        q_hi = sqrt(b.pump.shutoff_at(b.speed_rpm) / b.pump.curvature)
        state, opening = "steady", None
        if excess(q_in, 1.0) < 0:                              # 밸브를 다 열어도 유입만큼 못 뺀다 → 액위 상승
            state, opening = "level_rising", 1.0
            q_out = root(lambda q: excess(q, 1.0), min(q_in, q_hi)) if excess(0.0, 1.0) > 0 else 0.0
        elif q_in > 0 and excess(q_in, d.min_opening) > 0:     # 최소 개도로도 더 빠진다 → 액위 하강
            state, opening = "level_falling", d.min_opening
            q_out = root(lambda q: excess(q, d.min_opening), q_hi)
        else:
            q_out = q_in
            p = suction(q_in)
            need = (p + self.boost(branch, q_in, p)[0] - branch.outlet_abs_pa) / q_in**2 - r_pipe if q_in > 0 else r_valve0
            opening = min(1.0, d.normal_opening * sqrt(r_valve0 / need)) if need > 0 else 1.0
        p_s = suction(q_out)
        if p_s <= 0:
            raise ValueError("후속 펌프 흡입 절대압이 양수여야 합니다.")
        r_valve = r_valve0 * (d.normal_opening / opening) ** 2
        return {"q_out": q_out, "suction": p_s, "opening": opening, "state": state,
                "discharge_loss_pa": (r_pipe + r_valve) * q_out**2}

    def results(self, discharge: float, flows: list[float]) -> list[BranchResult]:
        results = []
        for i, branch in enumerate(self.network.branches):
            f, r, tail, dz = flows[i], self.r[i], self.tails[i], self.elevations[i]
            b = branch.booster
            if branch.drum is not None:
                results.append(self._drum_result(i, discharge, f))
                continue
            p_bs = discharge - dz - r * f**2 if b else None
            if p_bs is not None and p_bs <= 0:
                raise ValueError("후속 펌프 흡입 절대압이 양수여야 합니다.")
            extra = {}
            dp, bl = self.boost(branch, f, p_bs) if b else (0.0, 0.0)
            if b is not None:
                b_eta = b.pump.efficiency * (1 - (b.npsh.efficiency_coupling if b.npsh else 0.0) * bl)
                extra = {"booster_suction_abs_pa": p_bs, "booster_discharge_abs_pa": branch.outlet_abs_pa + tail * f**2,
                         "booster_hydraulic_w": f * dp, "booster_shaft_w": f * dp / b_eta,
                         "booster_strainer_loss_pa": self.booster_r * f**2}
                if b.npsh is not None:
                    _, na, nr, mg, bl2 = b.npsh.evaluate(f, p_bs, self.density, b.speed_rpm / b.pump.reference_speed_rpm,
                                                         b.pump.rated_flow_m3_s)
                    if na <= 0 or (bl2 >= b.npsh.max_head_loss and not collapse_enabled()):
                        raise ValueError(f"캐비테이션 모델 유효 범위 밖: 후속 펌프 NPSHa {na:.2f} m, 양정 감소 {bl2:.2f}")
                    extra.update(booster_npsha_m=na, booster_npshr_m=nr, booster_npsh_margin=mg, booster_head_loss=bl2)
            loss_pa = (r + tail) * f**2
            results.append(BranchResult(
                branch_id=branch.branch_id, outlet_node=branch.outlet_node, outlet_abs_pa=branch.outlet_abs_pa,
                flow_m3_s=f, friction_loss_pa=loss_pa, elevation_pressure_pa=dz,
                pressure_residual_pa=discharge + dp - branch.outlet_abs_pa - dz - loss_pa, **extra))
        return results

    def _drum_result(self, i: int, discharge: float, q_in: float) -> BranchResult:
        branch, r, dz = self.network.branches[i], self.r[i], self.elevations[i]
        b = branch.booster
        out = self.drum_out(i, q_in)
        q, p_s = out["q_out"], out["suction"]
        dp, bl = self.boost(branch, q, p_s)
        b_eta = b.pump.efficiency * (1 - (b.npsh.efficiency_coupling if b.npsh else 0.0) * bl)
        extra = {"booster_suction_abs_pa": p_s, "booster_discharge_abs_pa": p_s + dp,
                 "booster_hydraulic_w": q * dp, "booster_shaft_w": q * dp / b_eta if q > 0 else 0.0,
                 "booster_flow_m3_s": q, "level_valve_opening": out["opening"], "drum_state": out["state"],
                 "booster_strainer_loss_pa": self.booster_r * q * q}
        if b.npsh is not None:
            _, na, nr, mg, bl2 = b.npsh.evaluate(q, p_s, self.density, b.speed_rpm / b.pump.reference_speed_rpm,
                                                 b.pump.rated_flow_m3_s)
            if na <= 0 or (bl2 >= b.npsh.max_head_loss and not collapse_enabled()):
                raise ValueError(f"캐비테이션 모델 유효 범위 밖: 후속 펌프 NPSHa {na:.2f} m, 양정 감소 {bl2:.2f}")
            extra.update(booster_npsha_m=na, booster_npshr_m=nr, booster_npsh_margin=mg, booster_head_loss=bl2)
        # 정상 상태(유출 = 유입)면 헤더 → 출구 압력 수지가 닫힌다. 액위가 변하는 상태는 그 차이가 잔차로 남는다.
        loss_pa = r * q_in**2 + self.booster_r * q * q + out["discharge_loss_pa"]
        return BranchResult(
            branch_id=branch.branch_id, outlet_node=branch.outlet_node, outlet_abs_pa=branch.outlet_abs_pa,
            flow_m3_s=q_in, friction_loss_pa=loss_pa, elevation_pressure_pa=dz,
            pressure_residual_pa=(discharge - dz - r * q_in**2 - branch.drum.inlet_abs_pa)
            + (p_s + dp - out["discharge_loss_pa"] - branch.outlet_abs_pa), **extra)
