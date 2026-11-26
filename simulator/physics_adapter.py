"""플랜트 대표 수력 회로의 계산값을 기존 태그와 고장 사건에 연결한다.

회로 모양: 입력 펌프 두 대(source, 병렬 CDU·RFCC / 직렬 HOU·RHDS·VRDS) → 공통 토출 헤더 → 등가 분기 둘, 그중 한 분기에 후속 펌프(downstream).
설비·태그·경계 조건은 플랜트마다 `config/physics_<plant>_integrated.yaml` 과 마스터에서 따로 읽는다.
"""
from dataclasses import replace
from functools import lru_cache
from itertools import product
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from common.config_models import ConfigError, PlantConfig, StrictModel, TagKey, load_yaml
from simulator.fault import FaultEvent, FaultMode, HydraulicParameter, TagPlan, event_tag_effect, hydraulic_plan, plan
from simulator.physics import KGF_CM2_PA
from simulator.physics.__main__ import NetworkCase
from simulator.physics.network import NetworkResult, OperatingPoint, VirtualID
from simulator.physics.pump import Positive
from simulator.physics_noise import Disturbance, loadings
from simulator.physics.station import Station, StationResult, Unit, UnitResult, UnitState

Fraction = Annotated[float, Field(gt=0, lt=1)]
Role = Literal["source", "downstream"]


class PumpStation(StrictModel):
    """입력 펌프 두 대 (PU-D5a, synthetic). 첫 펌프가 CoupledCase.equip_id 이고 두 번째의 최대 막힘 저항은 따로 둔다."""
    topology: Literal["series", "parallel"]
    units: list[Unit] = Field(min_length=2, max_length=2)
    max_second_strainer_resistance_pa_s2_m6: Positive


class CoupledResult(NetworkResult):
    """첫 입력 펌프는 NetworkResult 의 pump_* 로, 두 대는 units 로 본다. discharge_abs_pa 는 첫 펌프 토출압, 헤더는 header_abs_pa."""
    header_abs_pa: float
    total_flow_m3_s: float
    energy_residual_w: float
    units: list[UnitResult]


class CoupledCase(NetworkCase):
    downstream_path: list[str] = Field(min_length=2)
    downstream_branch: VirtualID
    control_valve: VirtualID
    atmospheric_pa: Positive
    max_strainer_resistance_pa_s2_m6: Positive
    max_impeller_head_loss: Fraction
    max_impeller_efficiency_loss: Fraction
    max_valve_opening_loss: Fraction
    # 후속 펌프의 흡입 스트레이너 막힘과 임펠러 마모 (synthetic assumption)
    max_booster_strainer_resistance_pa_s2_m6: Positive
    max_booster_head_loss: Fraction
    max_booster_efficiency_loss: Fraction
    station: PumpStation

    @model_validator(mode="after")
    def check_station(self) -> "CoupledCase":
        first = self.station.units[0]
        if first.unit_id != self.equip_id or first.pump != self.network.pump or first.npsh != self.network.pump_npsh:
            raise ValueError("station 의 첫 펌프는 equip_id·network.pump·pump_npsh 와 같아야 합니다.")
        return self

    @property
    def second(self) -> str:
        return self.station.units[1].unit_id

    def supply(self) -> Station:
        return Station(topology=self.station.topology, units=self.station.units, demand=self.network)

    def scenario(self, clog: float = 0.0, wear: float = 0.0, restriction: float = 0.0, booster_clog: float = 0.0,
                 booster_wear: float = 0.0, second_clog: float = 0.0, second_wear: float = 0.0,
                 ) -> tuple[Station, OperatingPoint, dict[str, UnitState], float]:
        """0~1 크기의 물리 파라미터 → (후속 펌프 마모를 반영한 회로, 운전점, 입력 펌프별 상태, 후속 펌프 흡입측 저항).
        시뮬레이터·보정 스크립트·Modelica 비교가 같은 입력을 쓴다."""
        branches = []
        for branch in self.network.branches:
            if branch.branch_id == self.downstream_branch and booster_wear:
                booster = branch.booster
                factor = 1 - booster_wear * self.max_booster_head_loss
                branch = branch.model_copy(update={"booster": booster.model_copy(update={"pump": booster.pump.model_copy(update={
                    "shutoff_head_m": booster.pump.shutoff_head_m * factor,
                    "rated_head_m": booster.pump.rated_head_m * factor,
                    "efficiency": booster.pump.efficiency * (1 - booster_wear * self.max_booster_efficiency_loss),
                })})})
            branches.append(branch)
        states = {u: UnitState(suction_resistance=c * r, head_loss=w * self.max_impeller_head_loss,
                               efficiency_loss=w * self.max_impeller_efficiency_loss)
                  for u, c, w, r in ((self.equip_id, clog, wear, self.max_strainer_resistance_pa_s2_m6),
                                     (self.second, second_clog, second_wear, self.station.max_second_strainer_resistance_pa_s2_m6))
                  if c or w}
        point = self.default_point
        opening = point.valve_openings.get(self.control_valve, 1.0)
        point = point.model_copy(update={"valve_openings": {
            **point.valve_openings, self.control_valve: opening * (1 - restriction * self.max_valve_opening_loss)}})
        station = self.supply().model_copy(update={"demand": self.network.model_copy(update={"branches": branches})})
        return station, point, states, booster_clog * self.max_booster_strainer_resistance_pa_s2_m6

    def solve(self, *amounts: float) -> CoupledResult:
        station, point, states, booster_r = self.scenario(*amounts)
        return self.result(point, station.solve(point, states, booster_suction_resistance=booster_r))

    @staticmethod
    def result(point: OperatingPoint, r: StationResult) -> CoupledResult:
        first = r.units[0]
        stopped = [u.unit_id for u in r.units if u.status != "running"]
        if stopped:
            raise ValueError(f"순방향 유량이 없는 입력 펌프: {', '.join(stopped)}")
        return CoupledResult(
            suction_abs_pa=first.suction_abs_pa, discharge_abs_pa=first.discharge_abs_pa, speed_rpm=point.speed_rpm,
            pump_flow_m3_s=first.flow_m3_s, pump_head_m=first.head_m, hydraulic_w=first.hydraulic_w, shaft_w=first.shaft_w,
            mass_residual_kg_s=r.mass_residual_kg_s,
            pump_pressure_residual_pa=max((u.pressure_residual_pa for u in r.units), key=abs), branches=r.branches,
            pump_npsha_m=first.npsha_m, pump_npshr_m=first.npshr_m, pump_npsh_margin=first.npsh_margin,
            pump_head_loss=first.cavitation_head_loss,
            header_abs_pa=r.header_abs_pa, total_flow_m3_s=r.flow_m3_s, energy_residual_w=r.energy_residual_w, units=r.units)


# 마스터 태그 단위 → 내부 SI 변환. 압력은 게이지 표기, 내부는 절대압 Pa.
PRESSURE_PA = {"kg/cm2(g)": KGF_CM2_PA, "bar(g)": 100_000.0}
FLOW_UNIT = "m3/h"
CURRENT_UNIT = "A"
# 역할별 필수·선택 신호. 후속 펌프 흡입압·전류는 마스터에 태그가 있을 때만 계산값을 쓴다.
REQUIRED = {"source": ("suction_pressure", "discharge_pressure", "flow", "current"),
            "downstream": ("discharge_pressure", "flow")}
# 스트레이너 차압·스트레이너 앞(흡입 상류) 압력은 2026-11-27 추가한 선택 계측이다(원인 판별: 막힘 vs 흡입 경계압 vs 회전수).
OPTIONAL = {"source": ("strainer_dp", "strainer_inlet_pressure"),
            "downstream": ("suction_pressure", "current", "strainer_dp", "strainer_inlet_pressure")}
DP_UNIT = "kPa"
# 역할별로 이 회로가 계산할 수 있는 물리 파라미터. 밸브 제한은 제품 분기 밸브 하나라 첫 입력 펌프에만 둔다(physical_parameters).
ROLE_PARAMETERS: dict[Role, frozenset[HydraulicParameter]] = {
    "source": frozenset({"strainer_blockage", "impeller_wear", "valve_restriction"}),
    "downstream": frozenset({"strainer_blockage", "impeller_wear"}),
}
Amounts = dict[tuple[str, HydraulicParameter], float]      # (설비 ID, 파라미터) → 크기
ParameterPlans = list[tuple[FaultEvent, str, dict[HydraulicParameter, TagPlan]]]


AMOUNTS = 7      # solve 인자 수: 첫 펌프 막힘·마모, 제품 밸브, 후속 펌프 막힘·마모, 둘째 펌프 막힘·마모


def _signal_unit_ok(signal: str, unit: str) -> bool:
    if signal in ("suction_pressure", "discharge_pressure", "strainer_inlet_pressure"):
        return unit in PRESSURE_PA
    if signal == "strainer_dp":
        return unit == DP_UNIT
    return unit == (FLOW_UNIT if signal == "flow" else CURRENT_UNIT)


class PhysicsAdapter:
    noise = None        # 정상 흔들림의 원인 변동은 묶음(PhysicsBundle) 설정에만 있다

    def __init__(self, plant: PlantConfig, path: Path) -> None:
        self.plant = plant
        self.control = plant.control_by_key()
        try:
            self.case = CoupledCase.model_validate(load_yaml(path))
            self._bind()
            # 정상 운전과 각 파라미터의 최대 변화에서 순방향 해가 있어야 한다. 장부(AnomalyBook)가 회로당 물리 사건을
            # 하나만 열기 때문에 역할 사이의 조합은 생기지 않는다. 입력 펌프의 기존 세 파라미터 조합도 계속 확인한다.
            for values in product((0.0, 1.0), repeat=3):
                self.solve(*values)
            for i in range(3, AMOUNTS):
                self.solve(*(float(i == j) for j in range(AMOUNTS)))
        except (OSError, ValueError, KeyError, StopIteration) as exc:
            raise ConfigError(f"physics {path}: {exc}") from exc

    def _bind(self) -> None:
        case = self.case
        self.primary = case.equip_id
        self.downstream = case.downstream_path[-1]
        self.second = case.second
        series = case.station.topology == "series"
        self.roles: dict[str, Role] = {self.primary: "source", self.second: "source", self.downstream: "downstream"}
        equipment = self.plant.equipment_by_id()
        process = {(a, b) for a, b, _, stream in self.plant.flow.edges if stream == "process"}
        if (case.plant_id != self.plant.plant.plant_id or equipment[self.primary].unit_id != case.unit_id
                or self.downstream not in equipment or self.downstream == self.primary
                or case.downstream_path[0] != self.primary
                or len(self.roles) != 3 or self.second not in equipment or equipment[self.second].unit_id != case.unit_id
                # 직렬이면 둘째 펌프가 경로 위에, 병렬이면 첫 펌프와 같은 다음 노드로 이어져야 한다
                or (case.downstream_path[1] != self.second if series else (self.second, case.downstream_path[1]) not in process)
                or not set(zip(case.downstream_path, case.downstream_path[1:])) <= process):
            raise ValueError("물리 회로의 공장·Unit·하류 경로가 마스터와 일치해야 합니다.")
        self._check_path(case.downstream_path, set(equipment) - {self.second})
        branch = next((b for b in case.network.branches if b.branch_id == case.downstream_branch), None)
        if branch is None or branch.booster is None or case.control_valve not in branch.valves:
            raise ValueError("후속 펌프와 제어밸브는 선택한 제품 분기에 있어야 합니다.")

        self.keys: dict[str, dict[str, TagKey]] = {}
        self.pressure_pa: dict[TagKey, float] = {}
        for eid, role in self.roles.items():
            wanted = REQUIRED[role] + OPTIONAL[role]
            tags = {t.signal: t for t in equipment[eid].tags if t.signal in wanted}
            missing = [s for s in REQUIRED[role] if s not in tags]
            if missing or any(not _signal_unit_ok(s, t.unit) for s, t in tags.items()) \
                    or len(tags) != sum(t.signal in wanted for t in equipment[eid].tags):
                raise ValueError(f"{eid}: 물리 계측 신호({', '.join(REQUIRED[role])})와 단위가 마스터와 일치해야 합니다.")
            self.keys[eid] = {s: (eid, t.tag_id) for s, t in tags.items()}
            for s, t in tags.items():
                if s.endswith("_pressure"):
                    self.pressure_pa[(eid, t.tag_id)] = PRESSURE_PA[t.unit]
        self.tag_keys = {key for keys in self.keys.values() for key in keys.values()}
        self._solve_cached = lru_cache(maxsize=512)(self._solve)

    def _check_path(self, path: list[str], equipment: set[str]) -> None:
        """등가 배관 하나로 묶는 경로의 조건: 중간에 다른 계측 설비가 없고, 열교환기처럼 측(저온·고온)이 적힌 노드는 들어온 측으로만 나간다."""
        if set(path[1:-1]) & equipment:
            raise ValueError("물리 회로 경로의 중간에는 계측 설비를 둘 수 없습니다.")
        side = {(a, b): label for a, b, label, stream in self.plant.flow.edges if stream == "process"}
        for a, node, b in zip(path, path[1:], path[2:]):
            came, sides = side[(a, node)], {label for (x, _), label in side.items() if x == node}
            # 들어온 간선의 측 이름이 이 노드의 나가는 측 가운데 하나면 같은 측으로 나가야 한다
            if came in sides and came and side[(node, b)] != came:
                raise ValueError(f"물리 회로 경로가 {node} 의 {came}에서 {side[(node, b)] or '다른 측'}으로 건너갑니다.")

    def circuit_of(self, equip_id: str) -> str | None:
        return f"coupled:{self.primary}" if equip_id in self.roles else None

    def circuits(self) -> list[dict]:
        return [{"id": f"coupled:{self.primary}", "kind": "coupled", "equipment": [self.primary, self.second, self.downstream],
                 "source": self.primary, "sources": [self.primary, self.second], "topology": self.case.station.topology, "downstream": self.downstream, "path": self.case.downstream_path,
                 "unit_id": self.case.unit_id, "provenance": self.case.provenance,
                 # 입력 펌프나 후속 펌프에 NPSH 설정이 있으면 이 회로 해에 캐비테이션 양정 감소가 들어간다(network._solve_npsh).
                 "cavitation_model": self.case.network.pump_npsh is not None
                 or any(b.booster is not None and b.booster.npsh is not None for b in self.case.network.branches)}]

    # --- 사건 → 파라미터 -----------------------------------------------------------
    def physical_parameters(self, mode: FaultMode, equip_id: str) -> dict[HydraulicParameter, TagPlan]:
        """그 설비 역할에서 이 회로가 계산하는 물리 파라미터만 남긴다. 회로 밖 설비는 빈 dict."""
        role = self.roles.get(equip_id)
        if role is None:
            return {}
        return {name: tp for name, tp in hydraulic_plan(mode).items() if name in ROLE_PARAMETERS[role]
                and not (name == "valve_restriction" and equip_id != self.primary)}

    def tag_plan(self, mode: FaultMode, equip_id: str) -> dict[TagKey, TagPlan]:
        """기존 경험 태그는 유지하고, 물리 태그는 장부에 물리 적용으로 표시한다."""
        parameters = self.physical_parameters(mode, equip_id)
        result = {key: tp for key, tp in plan(self.plant, mode, equip_id, self.control).items()
                  if key not in self.tag_keys and (not parameters or tp.via != "process")}
        if parameters:
            applied = tuple(change for tp in parameters.values() for change in tp.applied)
            result.update({key: TagPlan(applied, (), via="physics", recovery=max(tp.recovery for tp in parameters.values())) for key in sorted(self.tag_keys)})
        return result

    def parameter_plans(self, events: list[FaultEvent]) -> ParameterPlans:
        return [(event, event.equip_id, params) for event in events
                if (params := self.physical_parameters(event.mode, event.equip_id))]

    def state(self, epoch: int, plans: ParameterPlans) -> CoupledResult:
        amounts: Amounts = {}
        for event, eid, parameters in plans:
            for name, tp in parameters.items():
                # 사건이 겹쳐도 검증한 동일 파라미터의 최대 이상 크기를 넘기지 않는다.
                amount, _ = event_tag_effect(tp, event, epoch)
                amounts[(eid, name)] = max(amounts.get((eid, name), 0.0), min(1.0, amount))
        return self.solve(*(amounts.get(key, 0.0) for key in (
            (self.primary, "strainer_blockage"), (self.primary, "impeller_wear"), (self.primary, "valve_restriction"),
            (self.downstream, "strainer_blockage"), (self.downstream, "impeller_wear"),
            (self.second, "strainer_blockage"), (self.second, "impeller_wear"))))

    # --- 수력 해 ----------------------------------------------------------------
    def solve(self, *amounts: float) -> CoupledResult:
        return self._solve_cached(*amounts, *(0.0,) * (AMOUNTS - len(amounts)))

    def _solve(self, *amounts: float) -> CoupledResult:
        return self.case.solve(*amounts)

    def _gauge(self, key: TagKey, abs_pa: float) -> float:
        return (abs_pa - self.case.atmospheric_pa) / self.pressure_pa[key]

    def sensor_centers(self, state: CoupledResult) -> dict[TagKey, float]:
        normal = self.solve()
        product = next(b for b in state.branches if b.branch_id == self.case.downstream_branch)
        ref_product = next(b for b in normal.branches if b.branch_id == self.case.downstream_branch)
        downstream = self.keys[self.downstream]
        centers = {
            downstream["discharge_pressure"]: self._gauge(downstream["discharge_pressure"], product.booster_discharge_abs_pa),
            downstream["flow"]: _outflow(product) * 3600,
        }
        for unit, ref in zip(state.units, normal.units):
            keys = self.keys[unit.unit_id]
            centers.update({
                keys["suction_pressure"]: self._gauge(keys["suction_pressure"], unit.suction_abs_pa),
                keys["discharge_pressure"]: self._gauge(keys["discharge_pressure"], unit.discharge_abs_pa),
                keys["flow"]: unit.flow_m3_s * 3600,
                keys["current"]: self.control[keys["current"]][0] * unit.shaft_w / ref.shaft_w,
            })
            centers.update(strainer_centers(self, keys, unit))
        if "suction_pressure" in downstream:
            centers[downstream["suction_pressure"]] = self._gauge(downstream["suction_pressure"], product.booster_suction_abs_pa)
        if "current" in downstream:
            centers[downstream["current"]] = (self.control[downstream["current"]][0]
                                              * product.booster_shaft_w / ref_product.booster_shaft_w)
        centers.update(booster_strainer_centers(self, downstream, product))
        return centers


def strainer_centers(adapter: "PhysicsAdapter", keys: dict[str, TagKey], unit) -> dict[TagKey, float]:
    """스트레이너 차압(kPa)과 스트레이너 앞 압력(게이지). 태그가 있을 때만. 엔진 정상 해의 흡입 저항 손실은 0 이므로
    깨끗한 스트레이너 차압(마스터 μ, 가정값)을 더해 정상 해가 마스터 운전점과 같게 한다."""
    return _strainer(adapter, keys, unit.suction_loss_pa or 0.0, unit.suction_abs_pa)


def _strainer(adapter: "PhysicsAdapter", keys: dict[str, TagKey], loss: float, suction_abs_pa: float | None) -> dict[TagKey, float]:
    out = {}
    if "strainer_dp" not in keys:
        return out
    clean_kpa = adapter.control[keys["strainer_dp"]][0]
    out[keys["strainer_dp"]] = clean_kpa + loss / 1000.0
    if "strainer_inlet_pressure" in keys and suction_abs_pa is not None:
        out[keys["strainer_inlet_pressure"]] = adapter._gauge(keys["strainer_inlet_pressure"],
                                                             suction_abs_pa + clean_kpa * 1000.0 + loss)
    return out


def booster_strainer_centers(adapter: "PhysicsAdapter", keys: dict[str, TagKey], product) -> dict[TagKey, float]:
    """후속 펌프 앞 스트레이너 차압(kPa)·스트레이너 앞 압력(게이지). 태그가 있을 때만."""
    return _strainer(adapter, keys, product.booster_strainer_loss_pa or 0.0, product.booster_suction_abs_pa)


def _outflow(branch) -> float:
    """후속 펌프 유량. 드럼 분기는 드럼 유출(후속 펌프), 아니면 분기 유량."""
    return branch.booster_flow_m3_s if branch.booster_flow_m3_s is not None else branch.flow_m3_s


# --- 여러 회로 묶음 ---------------------------------------------------------------
class PhysicsBundle:
    """한 공장의 독립 회로 여러 개. YAML `circuits: [파일, …]` 로 정의한다(경로는 그 YAML 기준).
    회로끼리는 경계 조건으로만 닿고 서로 다시 풀지 않는다. 기존 단일 결합 회로 API 와 같은 이름을 제공한다."""

    def __init__(self, plant: PlantConfig, path: Path, general: bool = False) -> None:
        """general: 일반 모드. 회로가 계산하지 않는 경험 효과와 흐름 그래프 전파를 유지한다."""
        from simulator.physics_single import SingleAdapter, SingleCase
        self.plant = plant
        self.general = general
        self.control = plant.control_by_key()
        bundle = load_yaml(path)
        files = bundle.get("circuits") or []
        self.exclusions: dict[str, str] = dict(bundle.get("exclusions") or {})
        # D2-A 명시 매핑: 설비 → 이 트윈에서 물리 계산하는 기존 고장 카탈로그 ID. 없으면(None) 회로 파라미터로만 판단한다.
        self.scenarios: dict[str, list[str]] | None = bundle.get("scenarios")
        self.scenario_exclusions: dict[str, str] = dict(bundle.get("scenario_exclusions") or {})
        if not files:
            raise ConfigError(f"physics {path}: circuits 가 비어 있습니다.")
        self.adapters = []
        for name in files:
            file = path.parent / name
            data = load_yaml(file)
            self.adapters.append(SingleAdapter(plant, file) if "machine" in data else PhysicsAdapter(plant, file))
        # 정상 흔들림의 원인 변동 (PU-D5a). 없으면 태그별 잡음 그대로
        spec = bundle.get("disturbance")
        coupled = next((a for a in self.adapters if isinstance(a, PhysicsAdapter)), None)
        if spec is not None and coupled is None:
            raise ConfigError(f"physics {path}: disturbance 는 결합 회로에만 쓸 수 있습니다.")
        self.noise = loadings(coupled, Disturbance.model_validate(spec)) if spec is not None else None
        self.roles: dict[str, str] = {}
        self._owner = {}
        for adapter in self.adapters:
            for eid, role in adapter.roles.items():
                if eid in self.roles:
                    raise ConfigError(f"physics {path}: {eid} 가 두 회로에 있습니다.")
                self.roles[eid] = role
                self._owner[eid] = adapter
        self.tag_keys = {key for adapter in self.adapters for key in adapter.tag_keys}
        coupled = next((a for a in self.adapters if isinstance(a, PhysicsAdapter)), self.adapters[0])
        # 기존 호출부(정답 장부·목록) 호환: 첫 결합 회로의 입력·후속 설비와 설정
        self.primary = coupled.primary
        self.downstream = getattr(coupled, "downstream", None)
        self.case = coupled.case

    def circuit_of(self, equip_id: str) -> str | None:
        owner = self._owner.get(equip_id)
        return owner.circuit_of(equip_id) if owner else None

    def circuits(self) -> list[dict]:
        return [c for adapter in self.adapters for c in adapter.circuits()]

    def physical_parameters(self, mode: FaultMode, equip_id: str):
        owner = self._owner.get(equip_id)
        if owner is None or (self.scenarios is not None and mode.id not in self.scenarios.get(equip_id, [])):
            return {}
        return owner.physical_parameters(mode, equip_id)

    def tag_plan(self, mode: FaultMode, equip_id: str) -> dict[TagKey, TagPlan]:
        owner = self._owner.get(equip_id)
        if self.general:
            full = plan(self.plant, mode, equip_id, self.control)
            if not self.physical_parameters(mode, equip_id):
                return full         # 회로가 계산하지 않는 사건: 경험 효과를 회로 해 위에 얹는다
            # 회로가 계산하는 사건: 그 회로의 물리 태그는 해만 따르고 나머지 태그는 기존 효과·전파 그대로
            applied = {key: tp for key, tp in owner.tag_plan(mode, equip_id).items() if tp.via == "physics"}
            # 교호작용(I3 의 유량)은 회로에 없으므로 키운 만큼만 해 위에 더한다. 온도 상승을 회로가 계산하는 사건은 이미 해에 들어 있다.
            if "suction_temperature_rise" not in self.physical_parameters(mode, equip_id):
                for key in applied.keys() & full.keys():
                    boost = full[key].boost
                    if boost is not None and boost.by is not None and boost.by not in owner.tag_keys:
                        boost = replace(boost, only=True)
                        applied[key] = replace(applied[key], boost=boost, extra=replace(full[key], boost=boost, quiet=()))
            return {key: tp for key, tp in full.items() if key not in owner.tag_keys} | applied
        result = owner.tag_plan(mode, equip_id) if owner else plan(self.plant, mode, equip_id, self.control)
        own = owner.tag_keys if owner else set()
        if owner is not None and not self.physical_parameters(mode, equip_id):
            # 명시 매핑에 없는 모드: 회로 물리 파라미터를 쓰지 않는 경험 신호 계획(물리 태그 제외)으로 낮춘다.
            result = {key: tp for key, tp in plan(self.plant, mode, equip_id, self.control).items() if key not in self.tag_keys}
        # 다른 회로의 물리 태그에는 경험 효과(z)를 얹지 않는다. 그 태그는 그 회로의 해만 따른다.
        # 공정 그래프를 따라가는 경험 전파(process)도 트윈에는 없다: 설비 사이 변화는 회로 해와 축(driven)만 보인다 (2026-11-25 D3).
        return {key: tp for key, tp in result.items()
                if tp.via != "process" and (key not in self.tag_keys or (key in own and tp.via == "physics"))}

    def parameter_plans(self, events: list[FaultEvent]):
        # 명시 매핑에 없는 사건은 경험 효과만 쓰므로 회로 입력에서도 뺀다 (한 태그에 두 번 걸리지 않게).
        events = [e for e in events if self.physical_parameters(e.mode, e.equip_id)]
        return [adapter.parameter_plans(events) for adapter in self.adapters]

    def state(self, epoch: int, plans):
        return [adapter.state(epoch, p) for adapter, p in zip(self.adapters, plans)]

    def sensor_centers(self, states) -> dict[TagKey, float]:
        out: dict[TagKey, float] = {}
        for adapter, state in zip(self.adapters, states):
            out.update(adapter.sensor_centers(state))
        return out


def load_physics(plant: PlantConfig, path: Path, general: bool = False):
    """runtime.physics 파일 → 단일 결합 회로(PhysicsAdapter) 또는 여러 회로 묶음(PhysicsBundle)."""
    try:
        data = load_yaml(path)
    except OSError as exc:
        raise ConfigError(f"physics {path}: {exc}") from exc
    return PhysicsBundle(plant, path, general) if "circuits" in data else PhysicsAdapter(plant, path)
