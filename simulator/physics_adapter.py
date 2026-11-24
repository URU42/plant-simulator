"""플랜트 대표 수력 회로의 계산값을 기존 태그와 고장 사건에 연결한다.

회로 모양은 다섯 플랜트가 같다: 입력 펌프(source) → 공통 토출 헤더 → 등가 분기 둘, 그중 한 분기에 후속 펌프(downstream).
설비·태그·경계 조건은 플랜트마다 `config/physics_<plant>_integrated.yaml` 과 마스터에서 따로 읽는다.
"""
from functools import lru_cache
from itertools import product
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from common.config_models import ConfigError, PlantConfig, TagKey, load_yaml
from simulator.fault import FaultEvent, FaultMode, HydraulicParameter, TagPlan, event_tag_effect, hydraulic_plan, plan
from simulator.physics import KGF_CM2_PA
from simulator.physics.__main__ import NetworkCase
from simulator.physics.network import Network, NetworkResult, OperatingPoint, VirtualID
from simulator.physics.pump import Positive

Fraction = Annotated[float, Field(gt=0, lt=1)]
Role = Literal["source", "downstream"]


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

    def scenario(self, clog: float = 0.0, wear: float = 0.0, restriction: float = 0.0, booster_clog: float = 0.0,
                 booster_wear: float = 0.0) -> tuple[Network, OperatingPoint, float, float]:
        """0~1 크기의 물리 파라미터 → (마모를 반영한 회로 사본, 운전점, 입력·후속 펌프 흡입측 저항).
        시뮬레이터·보정 스크립트·Modelica 비교가 같은 입력을 쓴다."""
        pump = self.network.pump
        head_factor = 1 - wear * self.max_impeller_head_loss
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
        network = self.network.model_copy(update={"branches": branches, "pump": pump.model_copy(update={
            "shutoff_head_m": pump.shutoff_head_m * head_factor,
            "rated_head_m": pump.rated_head_m * head_factor,
            "efficiency": pump.efficiency * (1 - wear * self.max_impeller_efficiency_loss),
        })})
        point = self.default_point
        opening = point.valve_openings.get(self.control_valve, 1.0)
        point = point.model_copy(update={"valve_openings": {
            **point.valve_openings, self.control_valve: opening * (1 - restriction * self.max_valve_opening_loss)}})
        return (network, point, clog * self.max_strainer_resistance_pa_s2_m6,
                booster_clog * self.max_booster_strainer_resistance_pa_s2_m6)

    def solve(self, *amounts: float) -> NetworkResult:
        network, point, suction_r, booster_r = self.scenario(*amounts)
        return network.solve(point, suction_r, booster_r)


# 마스터 태그 단위 → 내부 SI 변환. 압력은 게이지 표기, 내부는 절대압 Pa.
PRESSURE_PA = {"kg/cm2(g)": KGF_CM2_PA, "bar(g)": 100_000.0}
FLOW_UNIT = "m3/h"
CURRENT_UNIT = "A"
# 역할별 필수·선택 신호. 후속 펌프 흡입압·전류는 마스터에 태그가 있을 때만 계산값을 쓴다.
REQUIRED = {"source": ("suction_pressure", "discharge_pressure", "flow", "current"),
            "downstream": ("discharge_pressure", "flow")}
OPTIONAL = {"source": (), "downstream": ("suction_pressure", "current")}
# 역할별로 이 회로가 계산할 수 있는 물리 파라미터. 밸브 제한은 입력 펌프 쪽 제품 분기 밸브만 뜻한다.
ROLE_PARAMETERS: dict[Role, frozenset[HydraulicParameter]] = {
    "source": frozenset({"strainer_blockage", "impeller_wear", "valve_restriction"}),
    "downstream": frozenset({"strainer_blockage", "impeller_wear"}),
}
Amounts = dict[tuple[Role, HydraulicParameter], float]
ParameterPlans = list[tuple[FaultEvent, Role, dict[HydraulicParameter, TagPlan]]]


def _signal_unit_ok(signal: str, unit: str) -> bool:
    if signal in ("suction_pressure", "discharge_pressure"):
        return unit in PRESSURE_PA
    return unit == (FLOW_UNIT if signal == "flow" else CURRENT_UNIT)


class PhysicsAdapter:
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
            self.solve(0.0, 0.0, 0.0, 1.0, 0.0)
            self.solve(0.0, 0.0, 0.0, 0.0, 1.0)
        except (OSError, ValueError, KeyError, StopIteration) as exc:
            raise ConfigError(f"physics {path}: {exc}") from exc

    def _bind(self) -> None:
        case = self.case
        self.primary = case.equip_id
        self.downstream = case.downstream_path[-1]
        self.roles: dict[str, Role] = {self.primary: "source", self.downstream: "downstream"}
        equipment = self.plant.equipment_by_id()
        process = {(a, b) for a, b, _, stream in self.plant.flow.edges if stream == "process"}
        if (case.plant_id != self.plant.plant.plant_id or equipment[self.primary].unit_id != case.unit_id
                or self.downstream not in equipment or self.downstream == self.primary
                or case.downstream_path[0] != self.primary
                or not set(zip(case.downstream_path, case.downstream_path[1:])) <= process):
            raise ValueError("물리 회로의 공장·Unit·하류 경로가 마스터와 일치해야 합니다.")
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

    def circuit_of(self, equip_id: str) -> str | None:
        return f"coupled:{self.primary}" if equip_id in self.roles else None

    def circuits(self) -> list[dict]:
        return [{"id": f"coupled:{self.primary}", "kind": "coupled", "equipment": [self.primary, self.downstream],
                 "source": self.primary, "downstream": self.downstream, "path": self.case.downstream_path,
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
        return {name: tp for name, tp in hydraulic_plan(mode).items() if name in ROLE_PARAMETERS[role]}

    def tag_plan(self, mode: FaultMode, equip_id: str) -> dict[TagKey, TagPlan]:
        """기존 경험 태그는 유지하고, 물리 태그는 장부에 물리 적용으로 표시한다."""
        parameters = self.physical_parameters(mode, equip_id)
        result = {key: tp for key, tp in plan(self.plant, mode, equip_id, self.control).items()
                  if key not in self.tag_keys and (not parameters or tp.via != "process")}
        if parameters:
            applied = tuple(change for tp in parameters.values() for change in tp.applied)
            result.update({key: TagPlan(applied, (), via="physics") for key in sorted(self.tag_keys)})
        return result

    def parameter_plans(self, events: list[FaultEvent]) -> ParameterPlans:
        return [(event, self.roles[event.equip_id], params) for event in events
                if (params := self.physical_parameters(event.mode, event.equip_id))]

    def state(self, epoch: int, plans: ParameterPlans) -> NetworkResult:
        amounts: Amounts = {}
        for event, role, parameters in plans:
            for name, tp in parameters.items():
                # 사건이 겹쳐도 검증한 동일 파라미터의 최대 이상 크기를 넘기지 않는다.
                amount, _ = event_tag_effect(tp, event, epoch)
                amounts[(role, name)] = max(amounts.get((role, name), 0.0), min(1.0, amount))
        return self.solve(amounts.get(("source", "strainer_blockage"), 0.0),
                          amounts.get(("source", "impeller_wear"), 0.0),
                          amounts.get(("source", "valve_restriction"), 0.0),
                          amounts.get(("downstream", "strainer_blockage"), 0.0),
                          amounts.get(("downstream", "impeller_wear"), 0.0))

    # --- 수력 해 ----------------------------------------------------------------
    def solve(self, clog: float, wear: float, restriction: float,
              booster_clog: float = 0.0, booster_wear: float = 0.0) -> NetworkResult:
        return self._solve_cached(clog, wear, restriction, booster_clog, booster_wear)

    def _solve(self, clog: float, wear: float, restriction: float, booster_clog: float,
               booster_wear: float) -> NetworkResult:
        return self.case.solve(clog, wear, restriction, booster_clog, booster_wear)

    def _gauge(self, key: TagKey, abs_pa: float) -> float:
        return (abs_pa - self.case.atmospheric_pa) / self.pressure_pa[key]

    def sensor_centers(self, state: NetworkResult) -> dict[TagKey, float]:
        normal = self.solve(0.0, 0.0, 0.0)
        product = next(b for b in state.branches if b.branch_id == self.case.downstream_branch)
        ref_product = next(b for b in normal.branches if b.branch_id == self.case.downstream_branch)
        source, downstream = self.keys[self.primary], self.keys[self.downstream]
        centers = {
            source["suction_pressure"]: self._gauge(source["suction_pressure"], state.suction_abs_pa),
            source["discharge_pressure"]: self._gauge(source["discharge_pressure"], state.discharge_abs_pa),
            source["flow"]: state.pump_flow_m3_s * 3600,
            source["current"]: self.control[source["current"]][0] * state.shaft_w / normal.shaft_w,
            downstream["discharge_pressure"]: self._gauge(downstream["discharge_pressure"], product.booster_discharge_abs_pa),
            downstream["flow"]: product.flow_m3_s * 3600,
        }
        if "suction_pressure" in downstream:
            centers[downstream["suction_pressure"]] = self._gauge(downstream["suction_pressure"], product.booster_suction_abs_pa)
        if "current" in downstream:
            centers[downstream["current"]] = (self.control[downstream["current"]][0]
                                              * product.booster_shaft_w / ref_product.booster_shaft_w)
        return centers


# --- 여러 회로 묶음 ---------------------------------------------------------------
class PhysicsBundle:
    """한 공장의 독립 회로 여러 개. YAML `circuits: [파일, …]` 로 정의한다(경로는 그 YAML 기준).
    회로끼리는 경계 조건으로만 닿고 서로 다시 풀지 않는다. 기존 단일 결합 회로 API 와 같은 이름을 제공한다."""

    def __init__(self, plant: PlantConfig, path: Path) -> None:
        from simulator.physics_single import SingleAdapter, SingleCase
        self.plant = plant
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
        result = owner.tag_plan(mode, equip_id) if owner else plan(self.plant, mode, equip_id, self.control)
        own = owner.tag_keys if owner else set()
        if owner is not None and not self.physical_parameters(mode, equip_id):
            # 명시 매핑에 없는 모드: 회로 물리 파라미터를 쓰지 않는 경험 신호 계획(물리 태그 제외)으로 낮춘다.
            result = {key: tp for key, tp in plan(self.plant, mode, equip_id, self.control).items() if key not in self.tag_keys}
        # 다른 회로의 물리 태그에는 경험 효과(z)를 얹지 않는다. 그 태그는 그 회로의 해만 따른다.
        return {key: tp for key, tp in result.items()
                if key not in self.tag_keys or (key in own and tp.via == "physics")}

    def parameter_plans(self, events: list[FaultEvent]):
        return [adapter.parameter_plans(events) for adapter in self.adapters]

    def state(self, epoch: int, plans):
        return [adapter.state(epoch, p) for adapter, p in zip(self.adapters, plans)]

    def sensor_centers(self, states) -> dict[TagKey, float]:
        out: dict[TagKey, float] = {}
        for adapter, state in zip(self.adapters, states):
            out.update(adapter.sensor_centers(state))
        return out


def load_physics(plant: PlantConfig, path: Path):
    """runtime.physics 파일 → 단일 결합 회로(PhysicsAdapter) 또는 여러 회로 묶음(PhysicsBundle)."""
    try:
        data = load_yaml(path)
    except OSError as exc:
        raise ConfigError(f"physics {path}: {exc}") from exc
    return PhysicsBundle(plant, path) if "circuits" in data else PhysicsAdapter(plant, path)
