"""단일 기계 수력 회로: 흡입 경계 → 흡입 스트레이너·배관 → 펌프/팬 → 토출 배관 → 토출 경계.

D0 CDU 확장(2026-11-24)의 P-8303A(상압탑 하부 펌프, NPSH·캐비테이션 포함)와 F-8201(가열로 강제 통풍 팬)에 쓴다.
모든 계수·곡선·물성은 synthetic assumption 이며 현장 자료가 아니다. 정상 상태·단상·비압축(팬 차압/대기압 ≈ 2 %)이다.

캐비테이션 모델 (펌프, `npsh` 설정이 있을 때)
- 증기압: Clausius–Clapeyron 형 pv(T) = pv0 · exp(B · (1/T0 − 1/T)).
- NPSHa = (p_inlet − pv)/(ρg) + v²/(2g), v 는 흡입 노즐 평균 유속. p_inlet 은 스트레이너·흡입 배관 뒤 펌프 입구 절대압.
- NPSHr(Q, N) = (N/N0)² · NPSHr_rated · (c0 + (1 − c0) · (Q / (N/N0 · Q_rated))²).
- 여유비 s = NPSHa/NPSHr. 양정 감소율 d(s): s ≥ s0 에서 0, 1 ≤ s < s0 에서 0.03·x² (x = (s0 − s)/(s0 − 1)),
  s < 1 에서 0.03 + 0.06(x − 1) + k(x − 1)² (x = 1 에서 값·기울기 연속), 상한 d_max. NPSHr 정의(양정 3 % 저하)와 맞춘 합성 곡선이다.
- 양정 = (1 − d) · H(Q), 효율 = η · (1 − e·d). 기포 동역학·침식·진동·과도응답은 계산하지 않는다(진동은 기존 경험 신호).
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from math import exp, isfinite, pi, sqrt
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from common.config_models import ConfigError, PlantConfig, StrictModel, TagKey, load_yaml
from simulator.fault import FaultEvent, FaultMode, HydraulicParameter, TagPlan, event_tag_effect, hydraulic_plan, plan
from simulator.physics import KGF_CM2_PA
from simulator.physics.npsh import Npsh  # noqa: F401  (D0 위치에서 공용 모듈로 이동)
from simulator.physics.pipe import Pipe
from simulator.physics.pump import GRAVITY_M_S2, Positive

Fraction = Annotated[float, Field(gt=0, lt=1)]
MMH2O_PA = 9.80665
PRESSURE_PA = {"kg/cm2(g)": KGF_CM2_PA, "bar(g)": 100_000.0}


class Machine(StrictModel):
    """압력 상승 곡선 Δp(Q, N) = (N/N0)²·Δp0 − a·Q², a = (Δp0 − Δp_rated)/Q_rated²."""
    reference_speed_rpm: Positive
    speed_rpm: Positive
    rated_flow_m3_s: Positive
    shutoff_rise_pa: Positive
    rated_rise_pa: Positive
    efficiency: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _curve(self) -> "Machine":
        if self.rated_rise_pa >= self.shutoff_rise_pa:
            raise ValueError("정격 압력 상승은 체절 압력 상승보다 작아야 합니다.")
        return self

    @property
    def ratio(self) -> float:
        return self.speed_rpm / self.reference_speed_rpm

    @property
    def curvature(self) -> float:
        return (self.shutoff_rise_pa - self.rated_rise_pa) / self.rated_flow_m3_s**2

    def rise(self, q: float) -> float:
        return self.ratio**2 * self.shutoff_rise_pa - self.curvature * q**2


class SingleCase(StrictModel):
    plant_id: str
    unit_id: str
    equip_id: str
    kind: Literal["pump", "fan"]
    provenance: Literal["synthetic assumption"]
    suction_node: str                      # 흡입 경계 (온톨로지 노드 또는 virtual:)
    discharge_node: str
    atmospheric_pa: Positive
    density_kg_m3: Positive
    flow_reference_density_kg_m3: Positive | None = None   # Nm3/h 태그 환산용 표준 상태 밀도 (팬)
    source_abs_pa: Positive
    outlet_abs_pa: Positive
    machine: Machine
    suction_pipe: Pipe
    discharge_pipe: Pipe
    npsh: Npsh | None = None
    max_strainer_resistance_pa_s2_m6: Positive
    max_impeller_head_loss: Fraction
    max_impeller_efficiency_loss: Fraction
    max_temperature_rise_k: Positive | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "SingleCase":
        if (self.npsh is None) != (self.max_temperature_rise_k is None):
            raise ValueError("온도 상승 최대치는 NPSH 모델과 함께만 쓸 수 있습니다.")
        if self.kind == "fan" and self.npsh is not None:
            raise ValueError("팬에는 NPSH 모델을 쓰지 않습니다.")
        return self

    def scenario(self, clog: float = 0.0, wear: float = 0.0, temp_rise: float = 0.0) -> tuple["SingleCase", float, float]:
        """0~1 파라미터 → (마모를 반영한 사본, 스트레이너 저항, 흡입 유체 온도 K). Modelica 비교와 같은 입력."""
        machine = self.machine.model_copy(update={
            "shutoff_rise_pa": self.machine.shutoff_rise_pa * (1 - wear * self.max_impeller_head_loss),
            "rated_rise_pa": self.machine.rated_rise_pa * (1 - wear * self.max_impeller_head_loss),
            "efficiency": self.machine.efficiency * (1 - wear * self.max_impeller_efficiency_loss),
        })
        temp = self.npsh.fluid_temp_k + temp_rise * self.max_temperature_rise_k if self.npsh else 0.0
        return self.model_copy(update={"machine": machine}), clog * self.max_strainer_resistance_pa_s2_m6, temp

    def solve(self, clog: float = 0.0, wear: float = 0.0, temp_rise: float = 0.0) -> "SingleResult":
        case, strainer, temp = self.scenario(clog, wear, temp_rise)
        return case.solve_point(strainer, temp)

    # --- 운전점 -------------------------------------------------------------------
    def _at(self, q: float, strainer: float, temp: float) -> tuple[float, float, float, float, float, float]:
        """유량 q 에서 (입구 절대압, 증기압, NPSHa, NPSHr, 여유비, 양정 감소율)."""
        rho = self.density_kg_m3
        p_in = self.source_abs_pa - (strainer + self.suction_pipe.resistance(rho)) * q**2
        if self.npsh is None:
            return p_in, 0.0, 0.0, 0.0, 0.0, 0.0
        pv, npsha, npshr, margin, d = self.npsh.evaluate(q, p_in, rho, self.machine.ratio, self.machine.rated_flow_m3_s, temp)
        return p_in, pv, npsha, npshr, margin, d

    def _residual(self, q: float, strainer: float, temp: float) -> float:
        p_in, *_, d = self._at(q, strainer, temp)
        rise = self.machine.rise(q) * (1 - d)
        return p_in + rise - self.discharge_pipe.resistance(self.density_kg_m3) * q**2 - self.outlet_abs_pa

    def solve_point(self, strainer: float, temp: float) -> "SingleResult":
        if not isfinite(strainer) or strainer < 0:
            raise ValueError("스트레이너 저항은 유한한 음이 아닌 값이어야 합니다.")
        q_max = sqrt(self.machine.ratio**2 * self.machine.shutoff_rise_pa / self.machine.curvature)
        lo, hi = 0.0, q_max
        if self._residual(lo, strainer, temp) <= 0:
            raise ValueError("체절 압력이 토출 경계를 넘지 못해 순방향 유량이 없습니다.")
        # 유량이 늘면 흡입 손실·NPSHr 이 커지고 NPSHa·양정이 줄어 잔차가 단조 감소한다.
        for _ in range(200):
            mid = (lo + hi) / 2
            if mid in (lo, hi):
                break
            if self._residual(mid, strainer, temp) > 0:
                lo = mid
            else:
                hi = mid
        q = (lo + hi) / 2
        p_in, pv, npsha, npshr, margin, d = self._at(q, strainer, temp)
        if p_in <= 0:
            raise ValueError("흡입 절대압이 양수여야 합니다.")
        rise = self.machine.rise(q) * (1 - d)
        p_out = p_in + rise
        eta = self.machine.efficiency * (1 - (self.npsh.efficiency_coupling if self.npsh else 0.0) * d)
        hydraulic = q * rise
        rho = self.density_kg_m3
        dis_loss = self.discharge_pipe.resistance(rho) * q**2
        suc_loss = self.source_abs_pa - p_in
        shaft = hydraulic / eta
        return SingleResult(
            flow_m3_s=q, source_abs_pa=self.source_abs_pa, inlet_abs_pa=p_in, outlet_abs_pa=p_out,
            boundary_abs_pa=self.outlet_abs_pa, rise_pa=rise, hydraulic_w=hydraulic, shaft_w=shaft, efficiency=eta,
            vapor_pressure_pa=pv, fluid_temp_k=temp, npsha_m=npsha, npshr_m=npshr, npsh_margin=margin, head_loss=d,
            pressure_residual_pa=p_out - dis_loss - self.outlet_abs_pa,
            energy_residual_w=shaft * eta - q * (suc_loss + dis_loss) - q * (self.outlet_abs_pa - self.source_abs_pa),
        )


@dataclass(frozen=True)
class SingleResult:
    flow_m3_s: float
    source_abs_pa: float
    inlet_abs_pa: float
    outlet_abs_pa: float
    boundary_abs_pa: float
    rise_pa: float
    hydraulic_w: float
    shaft_w: float
    efficiency: float
    vapor_pressure_pa: float
    fluid_temp_k: float
    npsha_m: float
    npshr_m: float
    npsh_margin: float
    head_loss: float
    pressure_residual_pa: float
    energy_residual_w: float


SIGNALS = ("suction_pressure", "discharge_pressure", "differential_pressure", "flow", "current")
TEMP_UNITS = ("DEG C", "°C", "degC")
FLOW_UNITS = ("m3/h", "Nm3/h")
ParameterPlans = list[tuple[FaultEvent, dict[HydraulicParameter, TagPlan]]]


class SingleAdapter:
    """한 설비의 단일 회로를 기존 태그·고장 사건에 연결한다. 역할은 'single'."""

    def __init__(self, plant: PlantConfig, path: Path) -> None:
        self.plant = plant
        self.control = plant.control_by_key()
        try:
            self.case = SingleCase.model_validate(load_yaml(path))
            self._bind()
            self.solve(0, 0, 0)
            self.solve(1, 0, 0)
            self.solve(0, 1, 0)
            if self.case.npsh is not None:
                # 캐비테이션 모델의 유효 범위: 지원 시나리오 최대치에서 NPSHa > 0, 양정 감소가 상한에 닿지 않는다.
                # PM-SUCTION-HOT 은 S1 온도(1.0)와 S3 막힘(0.55)이 겹친다.
                for args in ((1, 0, 0), (0, 0, 1), (0.55, 0, 1), (0, 1, 1)):
                    r = self.solve(*args)
                    if r.npsha_m <= 0 or r.head_loss >= self.case.npsh.max_head_loss:
                        raise ValueError(f"캐비테이션 모델 유효 범위 밖 시나리오 {args}: NPSHa {r.npsha_m:.2f} m, 양정 감소 {r.head_loss:.2f}")
        except (OSError, ValueError, KeyError) as exc:
            raise ConfigError(f"physics {path}: {exc}") from exc

    def _bind(self) -> None:
        case = self.case
        equipment = self.plant.equipment_by_id()
        if case.plant_id != self.plant.plant.plant_id or case.equip_id not in equipment \
                or equipment[case.equip_id].unit_id != case.unit_id:
            raise ValueError("단일 회로의 공장·설비·Unit 이 마스터와 일치해야 합니다.")
        self.primary = case.equip_id
        self.circuit_id = f"single:{case.equip_id}"
        self.roles = {case.equip_id: "single"}
        # 윤활유 펌프는 토출압 태그 대신 oil_pressure 를 둔다(정적 계산 전용 회로 P-2501). 토출압으로 읽는다.
        signal = lambda t: "discharge_pressure" if t.signal == "oil_pressure" else t.signal     # noqa: E731
        tags = [t for t in equipment[case.equip_id].tags if signal(t) in SIGNALS]
        if len({signal(t) for t in tags}) != len(tags) or not any(t.signal == "flow" for t in tags):
            raise ValueError(f"{case.equip_id}: 물리 계측 신호가 중복되거나 유량 태그가 없습니다.")
        self.keys: dict[str, TagKey] = {}
        self.units: dict[str, str] = {}
        for t in tags:
            sig = signal(t)
            ok = (t.unit in PRESSURE_PA if sig.endswith("_pressure") and sig != "differential_pressure"
                  else t.unit == "mmH2O" if sig == "differential_pressure"
                  else t.unit in FLOW_UNITS if sig == "flow" else t.unit == "A")
            if not ok:
                raise ValueError(f"{case.equip_id}/{t.tag_id}: 지원하지 않는 단위 {t.unit}")
            if t.unit == "Nm3/h" and case.flow_reference_density_kg_m3 is None:
                raise ValueError("Nm3/h 유량 태그에는 표준 상태 밀도가 필요합니다.")
            self.keys[sig] = (case.equip_id, t.tag_id)
            self.units[sig] = t.unit
        self.tag_keys = set(self.keys.values())
        # 정적 계산 전용 출력(2026-11-27): 캐비테이션 모델(npsh)이 있으면 흡입 유체 온도 태그(process_temp, 설명에 '흡입')에
        # 흡입 온도 상승 원인 값이 증기압·NPSH 에 쓰는 유체 온도 변화를 그대로 낸다. 시간 생성기(tag_keys)에는 넣지 않는다:
        # 생성기는 이 태그를 경험식 효과(고장 카탈로그 delta 15 K)로 만들고, 다른 원인의 온도 상승·교호작용 I3 도 이 경로를 쓴다.
        self.calc_keys: dict[str, TagKey] = {}
        if case.npsh is not None:
            hot = [t for t in equipment[case.equip_id].tags
                   if t.signal == "process_temp" and "흡입" in (t.description or "") and t.unit in TEMP_UNITS]
            if len(hot) == 1:
                self.calc_keys["process_temp"] = (case.equip_id, hot[0].tag_id)
        self.parameters: frozenset[HydraulicParameter] = frozenset(
            {"strainer_blockage", "impeller_wear"} | ({"suction_temperature_rise"} if case.npsh else set()))
        self._solve_cached = lru_cache(maxsize=512)(lambda c, w, t: self.case.solve(c, w, t))

    def circuit_of(self, equip_id: str) -> str | None:
        return self.circuit_id if equip_id in self.roles else None

    def circuits(self) -> list[dict]:
        return [{"id": self.circuit_id, "kind": self.case.kind, "equipment": [self.primary], "source": self.primary,
                 "downstream": None, "path": [self.case.suction_node, self.primary, self.case.discharge_node],
                 "unit_id": self.case.unit_id, "provenance": self.case.provenance,
                 "cavitation_model": self.case.npsh is not None}]

    def physical_parameters(self, mode: FaultMode, equip_id: str) -> dict[HydraulicParameter, TagPlan]:
        if equip_id != self.primary:
            return {}
        return {name: tp for name, tp in hydraulic_plan(mode).items() if name in self.parameters}

    def tag_plan(self, mode: FaultMode, equip_id: str) -> dict[TagKey, TagPlan]:
        parameters = self.physical_parameters(mode, equip_id)
        result = {key: tp for key, tp in plan(self.plant, mode, equip_id, self.control).items()
                  if key not in self.tag_keys and (not parameters or tp.via != "process")}
        if parameters:
            applied = tuple(change for tp in parameters.values() for change in tp.applied)
            result.update({key: TagPlan(applied, (), via="physics", recovery=max(tp.recovery for tp in parameters.values())) for key in sorted(self.tag_keys)})
        return result

    def parameter_plans(self, events: list[FaultEvent]) -> ParameterPlans:
        return [(event, params) for event in events if (params := self.physical_parameters(event.mode, event.equip_id))]

    def state(self, epoch: int, plans: ParameterPlans) -> SingleResult:
        amounts: dict[str, float] = {}
        for event, parameters in plans:
            for name, tp in parameters.items():
                amount, _ = event_tag_effect(tp, event, epoch)
                amounts[name] = max(amounts.get(name, 0.0), min(1.0, amount))
        return self.solve(amounts.get("strainer_blockage", 0.0), amounts.get("impeller_wear", 0.0),
                          amounts.get("suction_temperature_rise", 0.0))

    def solve(self, clog: float, wear: float, temp_rise: float) -> SingleResult:
        return self._solve_cached(clog, wear, temp_rise)

    def sensor_centers(self, state: SingleResult, calc_only: bool = False) -> dict[TagKey, float]:
        """태그 중심값. calc_only 면 정적 계산 전용 출력(흡입 유체 온도)도 낸다."""
        atm = self.case.atmospheric_pa
        normal = self.solve(0.0, 0.0, 0.0)
        out: dict[TagKey, float] = {}
        if calc_only:
            for key in self.calc_keys.values():      # 온도 차(K = °C 차)를 운전점 μ 에 더한다
                out[key] = self.control[key][0] + (state.fluid_temp_k - normal.fluid_temp_k)
        for signal, key in self.keys.items():
            unit = self.units[signal]
            if signal == "suction_pressure":
                out[key] = (state.inlet_abs_pa - atm) / PRESSURE_PA[unit]
            elif signal == "discharge_pressure":
                out[key] = (state.outlet_abs_pa - atm) / PRESSURE_PA[unit]
            elif signal == "differential_pressure":
                out[key] = state.rise_pa / MMH2O_PA
            elif signal == "flow":
                factor = 1.0 if unit == "m3/h" else self.case.density_kg_m3 / self.case.flow_reference_density_kg_m3
                out[key] = state.flow_m3_s * 3600 * factor
            else:
                out[key] = self.control[key][0] * state.shaft_w / normal.shaft_w
        return out
