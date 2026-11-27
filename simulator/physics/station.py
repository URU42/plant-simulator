"""두 대 이상 펌프 묶음(직렬·병렬)과 기존 헤더 하류 회로의 정상 상태 해 (PU-D4).

- 직렬: 모든 단의 유량이 같고 앞 단 토출압에서 중간 손실을 뺀 값이 다음 단 흡입압이다.
- 병렬: 흡입 경계와 토출 헤더 압력을 공유한다. 펌프마다 체크밸브가 있고 역류하지 않는다.
하류(분기·밸브·후속 펌프)는 network.DemandSide, 펌프·NPSH·배관·밸브 식은 기존 모듈을 그대로 쓴다.
정적 해석이다. 기동·정지 과도, 수격, 체크밸브 동특성은 계산하지 않는다. 모든 제원은 synthetic assumption 이다.
"""
from math import isclose, isfinite, sqrt
from typing import Literal

from pydantic import Field, model_validator

from common.config_models import StrictModel
from simulator.physics.network import BranchResult, DemandSide, Network, OperatingPoint, root
from simulator.physics.npsh import Npsh, collapse_enabled
from simulator.physics.pipe import Pipe
from simulator.physics.pump import GRAVITY_M_S2, Nonnegative, Positive, Pump
from simulator.physics.valve import CheckValve, Valve

Reason = Literal["input", "blocked", "no_solution", "not_converged", "dry_run", "cavitation_range", "runout", "reverse"]


class SolveError(ValueError):
    """해를 내지 못한 이유를 구분한다. 값을 대신 만들지 않는다."""

    def __init__(self, reason: Reason, message: str) -> None:
        super().__init__(message)
        self.reason: Reason = reason


class Unit(StrictModel):
    unit_id: str
    pump: Pump
    npsh: Npsh | None = None
    line: Pipe                 # 펌프 토출 → 헤더(병렬) 또는 다음 단(직렬)
    check_valve: CheckValve
    valve: Valve               # 토출 차단밸브


class Stopped(StrictModel):
    """직렬에서 정지한 펌프의 처리. 근거 자료가 없어 기본값이 없다. 호출하는 쪽이 반드시 고른다.
    pass: 멈춘 임펠러를 저항으로 통과, bypass: 펌프·토출 라인 대신 우회 라인, block: 유로가 막힘."""
    mode: Literal["pass", "bypass", "block"]
    resistance_pa_s2_m6: Positive | None = None

    @model_validator(mode="after")
    def check(self) -> "Stopped":
        if (self.mode == "block") != (self.resistance_pa_s2_m6 is None):
            raise ValueError("pass·bypass 는 저항이 필요하고 block 은 저항을 받지 않습니다.")
        return self


class UnitState(StrictModel):
    speed_ratio: Positive = 1.0
    running: bool = True
    stopped: Stopped | None = None                # 직렬에서 running=False 일 때만
    valve_opening: float = Field(default=1.0, ge=0, le=1, allow_inf_nan=False)     # 0 은 완전 차단
    suction_resistance: Nonnegative = 0.0         # 개별 스트레이너(직렬 2단은 단 사이) Pa·s²/m⁶
    head_loss: float = Field(default=0.0, ge=0, lt=1)         # 임펠러 마모 양정 상대 감소
    efficiency_loss: float = Field(default=0.0, ge=0, lt=1)

    @model_validator(mode="after")
    def check(self) -> "UnitState":
        if self.running and self.stopped is not None:
            raise ValueError("운전 중인 펌프에는 정지 처리를 줄 수 없습니다.")
        return self


class UnitResult(StrictModel):
    unit_id: str
    status: Literal["running", "dead_headed", "stopped", "pass", "bypass"]
    flow_m3_s: float
    rated_flow_ratio: float
    suction_abs_pa: float
    discharge_abs_pa: float
    check_valve_open: bool
    reverse_dp_pa: float | None = None      # 닫힌 체크밸브가 막는 압력차(헤더 − 펌프 쪽)
    head_m: float | None = None
    hydraulic_w: float | None = None
    shaft_w: float | None = None            # dead_headed 는 고정 효율 모델로 계산할 수 없어 None
    npsha_m: float | None = None
    npshr_m: float | None = None
    npsh_margin: float | None = None
    cavitation_head_loss: float | None = None
    pressure_residual_pa: float = 0.0
    suction_loss_pa: float | None = None     # 흡입 저항(스트레이너 포함) 압력 손실. 흡입 상류 압력 = suction_abs_pa + 이 값 (2026-11-27)
    speed_rpm: float | None = None           # 운전 회전수(정지 0). 회전수 계측용 (2026-11-27)


class StationResult(StrictModel):
    flow_m3_s: float
    header_abs_pa: float
    units: list[UnitResult]
    branches: list[BranchResult]
    mass_residual_kg_s: float
    energy_residual_w: float


class Station(StrictModel):
    """topology 와 펌프 묶음 + 헤더 하류. demand.pump 와 demand.pump_npsh 는 쓰지 않는다."""
    topology: Literal["series", "parallel"]
    units: list[Unit] = Field(min_length=2)
    demand: Network

    @model_validator(mode="after")
    def check(self) -> "Station":
        if len({u.unit_id for u in self.units}) != len(self.units):
            raise ValueError("펌프 ID는 중복될 수 없습니다.")
        return self

    def solve(self, point: OperatingPoint, states: dict[str, UnitState] | None = None,
              common_suction_resistance: float = 0.0, booster_suction_resistance: float = 0.0) -> StationResult:
        """point.speed_rpm 은 모든 펌프의 기준 회전수에 대한 비율의 분자로 쓴다(단마다 speed_ratio 를 더 곱한다)."""
        try:
            return _Solver(self, point, states or {}, common_suction_resistance, booster_suction_resistance).run()
        except SolveError:
            raise
        except ValueError as exc:
            raise SolveError("cavitation_range" if "유효 범위" in str(exc) else "dry_run" if "양수여야" in str(exc) else "input",
                             str(exc)) from exc


class _Solver:
    def __init__(self, station: Station, point: OperatingPoint, states: dict[str, UnitState], common_r: float,
                 booster_r: float) -> None:
        unknown = states.keys() - {u.unit_id for u in station.units}
        if unknown:
            raise SolveError("input", f"등록되지 않은 펌프: {', '.join(sorted(unknown))}")
        if not isfinite(common_r) or common_r < 0:
            raise SolveError("input", "공통 흡입 저항은 유한한 음이 아닌 값이어야 합니다.")
        self.st, self.point, self.common_r = station, point, common_r
        self.rho = station.demand.density_kg_m3
        self.rho_g = self.rho * GRAVITY_M_S2
        self.side = DemandSide(station.demand, point, booster_r)
        self.state = {u.unit_id: states.get(u.unit_id, UnitState()) for u in station.units}
        for u in station.units:
            s = self.state[u.unit_id]
            if not s.running and (s.stopped is None) != (station.topology == "parallel"):
                raise SolveError("input", f"{u.unit_id}: 직렬 정지는 pass·bypass·block 가운데 하나를 골라야 하고, 병렬 정지는 고르지 않습니다.")

    # --- 펌프 하나 -------------------------------------------------------------------
    def speed(self, u: Unit) -> float:
        return self.point.speed_rpm * self.state[u.unit_id].speed_ratio

    def q_max(self, u: Unit) -> float:
        """양정이 0이 되는 유량."""
        return sqrt(u.pump.shutoff_at(self.speed(u)) / u.pump.curvature)

    def cavitation(self, u: Unit, q: float, p_in: float):
        if u.npsh is None:
            return None
        return u.npsh.evaluate(q, p_in, self.rho, self.speed(u) / u.pump.reference_speed_rpm, u.pump.rated_flow_m3_s)

    def head(self, u: Unit, q: float, p_in: float) -> float:
        """마모는 체절·정격 양정을 같은 비율로 줄인다(physics_adapter.scenario 와 같은 정의)."""
        s, cav = self.state[u.unit_id], self.cavitation(u, q, p_in)
        base = (u.pump.shutoff_at(self.speed(u)) - u.pump.curvature * q**2) * (1 - s.head_loss)
        return base * (1 - (cav[4] if cav else 0.0))

    def line_r(self, u: Unit) -> float:
        s = self.state[u.unit_id]
        return u.line.resistance(self.rho) + u.check_valve.resistance(self.rho) + u.valve.resistance(self.rho, s.valve_opening)

    def open(self, u: Unit) -> bool:
        return self.state[u.unit_id].valve_opening > 0

    # --- 묶음 공급 -------------------------------------------------------------------
    def parallel_flow(self, u: Unit, p_common: float, header: float) -> float:
        s = self.state[u.unit_id]
        if not s.running or not self.open(u):
            return 0.0
        line = self.line_r(u)

        def excess(q: float) -> float:
            p_in = p_common - s.suction_resistance * q**2
            return p_in + self.rho_g * self.head(u, q, p_in) - line * q**2 - header
        return root(excess, self.q_max(u))

    def parallel(self, header: float) -> tuple[float, list[float]]:
        """(공통 흡입압, 펌프별 유량)."""
        source = self.point.suction_abs_pa
        if not self.common_r:
            return source, [self.parallel_flow(u, source, header) for u in self.st.units]
        total = root(lambda q: sum(self.parallel_flow(u, source - self.common_r * q**2, header) for u in self.st.units) - q,
                     sum(self.q_max(u) for u in self.st.units))
        p = source - self.common_r * total**2
        return p, [self.parallel_flow(u, p, header) for u in self.st.units]

    def walk(self, q: float) -> tuple[float, list[tuple[float, float]]]:
        """직렬: (헤더 압력, 단별 (흡입압, 토출압))."""
        p, stages = self.point.suction_abs_pa - self.common_r * q**2, []
        for u in self.st.units:
            s = self.state[u.unit_id]
            if not s.running and s.stopped.mode == "bypass":
                stages.append((p, p - s.stopped.resistance_pa_s2_m6 * q**2))
                p = stages[-1][1]
                continue
            p_in = p - s.suction_resistance * q**2
            p_out = p_in + (self.rho_g * self.head(u, q, p_in) if s.running else -s.stopped.resistance_pa_s2_m6 * q**2)
            stages.append((p_in, p_out))
            p = p_out - self.line_r(u) * q**2
        return p, stages

    def series_limit(self) -> float:
        return min(self.q_max(u) for u in self.st.units if self.state[u.unit_id].running)

    def supply(self, header: float) -> float:
        if self.st.topology == "parallel":
            return sum(self.parallel(header)[1])
        return root(lambda q: self.walk(q)[0] - header, self.series_limit())

    # --- 해 -------------------------------------------------------------------------
    def run(self) -> StationResult:
        units, states = self.st.units, self.state
        series = self.st.topology == "series"
        if not any(states[u.unit_id].running for u in units):
            raise SolveError("blocked", "운전 중인 펌프가 없습니다.")
        blocked = [u.unit_id for u in units if series and (
            (not states[u.unit_id].running and states[u.unit_id].stopped.mode == "block")
            or (not self.open(u) and (states[u.unit_id].running or states[u.unit_id].stopped.mode != "bypass")))]
        if blocked or not any(states[u.unit_id].running and self.open(u) for u in units):
            raise SolveError("blocked", f"유로가 막혀 순방향 유량이 없습니다: {', '.join(blocked) or '모든 펌프'}")
        n = len(self.st.demand.branches)

        def surplus(p: float) -> float:
            return self.supply(p) - sum(self.side.demand(i, p) for i in range(n))

        hi = self.point.suction_abs_pa + self.rho_g * sum(u.pump.shutoff_at(self.speed(u)) for u in units if states[u.unit_id].running)
        lo = 1.0
        if surplus(lo) <= 0:
            raise SolveError("no_solution", "펌프 양정이 양수인 운전점이 없습니다.")
        for _ in range(200):
            mid = (lo + hi) / 2
            if mid in (lo, hi):
                break
            if surplus(mid) > 0:
                lo = mid
            else:
                hi = mid
        header = (lo + hi) / 2
        flows = [self.side.demand(i, header) for i in range(n)]
        p_common, unit_q = self.parallel(header) if not series else (0.0, [])
        q = self.supply(header)
        if q <= 0 or any(f <= 0 for f in flows):
            raise SolveError("reverse", "모든 하류 분기에 양의 순방향 유량이 필요합니다. 역류·폐쇄 분기는 지원하지 않습니다.")
        if not isclose(q, sum(flows), rel_tol=1e-9, abs_tol=1e-12):
            raise SolveError("not_converged", "유량 수지가 수치 허용 오차 내에서 수렴하지 않았습니다.")
        if series:
            end, stages = self.walk(q)
            results = [self.unit_result(u, q, *stage) for u, stage in zip(units, stages)]
            results[-1].pressure_residual_pa = end - header
            losses = q * (self.common_r * q**2) + sum(self.unit_loss(u, q) for u in units)
        else:
            results = [self.parallel_result(u, f, p_common, header) for u, f in zip(units, unit_q)]
            losses = q * self.common_r * q**2 + sum(self.unit_loss(u, f) for u, f in zip(units, unit_q) if f > 0)
        for u, r in zip(units, results):          # 스트레이너 차압·흡입 상류 압력 계측용 (bypass 는 흡입 저항을 거치지 않는다)
            st = states[u.unit_id]
            if r.flow_m3_s > 0 and (st.running or st.stopped.mode != "bypass"):
                r.suction_loss_pa = st.suction_resistance * r.flow_m3_s ** 2
        branches = self.side.results(header, flows)
        source = self.point.suction_abs_pa
        energy = (sum(r.hydraulic_w or 0.0 for r in results) + sum(b.booster_hydraulic_w or 0.0 for b in branches) - losses
                  - sum(b.flow_m3_s * (b.friction_loss_pa + b.elevation_pressure_pa + b.outlet_abs_pa - source)
                        # 드럼 분기: 손실·출구 항을 유입 유량으로 셌으므로 드럼에 쌓이거나(액위 상승) 빠지는 (유입 − 유출) 분은
                        # 후속 펌프 양정을 받지 않는다. 그 몫을 뺀다(정상 상태면 0).
                        - ((b.flow_m3_s - b.booster_flow_m3_s) * (b.booster_discharge_abs_pa - b.booster_suction_abs_pa)
                           if b.drum_state is not None else 0.0) for b in branches))
        return StationResult(flow_m3_s=q, header_abs_pa=header, units=results, branches=branches,
                             mass_residual_kg_s=self.rho * (q - sum(flows)), energy_residual_w=energy)

    def unit_loss(self, u: Unit, q: float) -> float:
        s = self.state[u.unit_id]
        if not s.running and s.stopped is not None:
            if s.stopped.mode == "bypass":
                return s.stopped.resistance_pa_s2_m6 * q**3
            return (s.stopped.resistance_pa_s2_m6 + s.suction_resistance + self.line_r(u)) * q**3
        return (s.suction_resistance + self.line_r(u)) * q**3

    def parallel_result(self, u: Unit, q: float, p_common: float, header: float) -> UnitResult:
        s = self.state[u.unit_id]
        if q > 0:
            p_in = p_common - s.suction_resistance * q**2
            r = self.unit_result(u, q, p_in, header + self.line_r(u) * q**2)
            r.pressure_residual_pa = p_in + self.rho_g * r.head_m - r.discharge_abs_pa
            return r
        if not s.running:
            return UnitResult(unit_id=u.unit_id, status="stopped", flow_m3_s=0.0, rated_flow_ratio=0.0, suction_abs_pa=p_common,
                              discharge_abs_pa=p_common, check_valve_open=False, reverse_dp_pa=header - p_common, hydraulic_w=0.0, shaft_w=0.0,
                              speed_rpm=0.0)
        head = self.head(u, 0.0, p_common)
        p_out = p_common + self.rho_g * head
        return UnitResult(unit_id=u.unit_id, status="dead_headed", flow_m3_s=0.0, rated_flow_ratio=0.0, suction_abs_pa=p_common,
                          discharge_abs_pa=p_out, check_valve_open=False,
                          reverse_dp_pa=header - p_out if self.open(u) else None, head_m=head, hydraulic_w=0.0, speed_rpm=self.speed(u))

    def unit_result(self, u: Unit, q: float, p_in: float, p_out: float) -> UnitResult:
        s = self.state[u.unit_id]
        if p_in <= 0 or p_out <= 0:
            raise SolveError("dry_run", f"{u.unit_id}: 흡입·토출 절대압이 양수여야 합니다.")
        ratio = q / u.pump.rated_flow_m3_s
        if not s.running:
            return UnitResult(unit_id=u.unit_id, status=s.stopped.mode, flow_m3_s=q if s.stopped.mode == "pass" else 0.0,
                              rated_flow_ratio=ratio if s.stopped.mode == "pass" else 0.0, suction_abs_pa=p_in, discharge_abs_pa=p_out,
                              check_valve_open=s.stopped.mode == "pass", hydraulic_w=0.0, shaft_w=0.0, speed_rpm=0.0)
        if q >= self.q_max(u) * (1 - 1e-9):
            raise SolveError("runout", f"{u.unit_id}: 유량이 양정 곡선 끝을 넘습니다.")
        head, cav, extra, loss = self.head(u, q, p_in), self.cavitation(u, q, p_in), {}, 0.0
        if cav:
            _, npsha, npshr, margin, loss = cav
            if npsha <= 0:
                raise SolveError("dry_run", f"{u.unit_id}: NPSHa {npsha:.2f} m (증기압 이하, 건운전 방지)")
            if loss >= u.npsh.max_head_loss and not collapse_enabled():
                raise SolveError("cavitation_range", f"{u.unit_id}: 캐비테이션 모델 유효 범위 밖(양정 감소 {loss:.2f})")
            extra = {"npsha_m": npsha, "npshr_m": npshr, "npsh_margin": margin, "cavitation_head_loss": loss}
        eta = u.pump.efficiency * (1 - s.efficiency_loss) * (1 - (u.npsh.efficiency_coupling if u.npsh else 0.0) * loss)
        hydraulic = q * self.rho_g * head
        return UnitResult(unit_id=u.unit_id, status="running", flow_m3_s=q, rated_flow_ratio=ratio, suction_abs_pa=p_in,
                          discharge_abs_pa=p_out, check_valve_open=True, head_m=head, hydraulic_w=hydraulic, shaft_w=hydraulic / eta,
                          speed_rpm=self.speed(u), **extra)
