"""CDU 유압 회로의 계산값을 기존 태그와 고장 사건에 연결한다."""
from functools import lru_cache
from itertools import product
from pathlib import Path
from typing import Annotated

from pydantic import Field

from common.config_models import ConfigError, PlantConfig, TagKey, load_yaml
from simulator.fault import FaultEvent, FaultMode, HydraulicParameter, TagPlan, event_tag_effect, hydraulic_plan, plan
from simulator.physics import KGF_CM2_PA
from simulator.physics.__main__ import NetworkCase
from simulator.physics.network import NetworkResult, VirtualID
from simulator.physics.pump import Positive

Fraction = Annotated[float, Field(gt=0, lt=1)]


class CoupledCase(NetworkCase):
    downstream_path: list[str] = Field(min_length=2)
    downstream_branch: VirtualID
    control_valve: VirtualID
    atmospheric_pa: Positive
    max_strainer_resistance_pa_s2_m6: Positive
    max_impeller_head_loss: Fraction
    max_impeller_efficiency_loss: Fraction
    max_valve_opening_loss: Fraction


# 기존 CDU 마스터의 신호와 단위를 직접 검증한다. 전류는 정격점 기준 축동력의 대용 계측값이다.
SIGNALS = {
    "suction_pressure": "kg/cm2(g)", "discharge_pressure": "kg/cm2(g)",
    "flow": "m3/h", "current": "A",
}


ParameterPlans = list[tuple[FaultEvent, dict[HydraulicParameter, TagPlan]]]


class PhysicsAdapter:
    def __init__(self, plant: PlantConfig, path: Path) -> None:
        self.plant = plant
        self.control = plant.control_by_key()
        try:
            self.case = CoupledCase.model_validate(load_yaml(path))
            self._bind()
            # 정상 운전과 정의된 최대 물리 변화 조합에서 모두 순방향 해가 있어야 한다.
            for values in product((0.0, 1.0), repeat=3):
                self.solve(*values)
        except (OSError, ValueError, KeyError, StopIteration) as exc:
            raise ConfigError(f"physics {path}: {exc}") from exc

    def _bind(self) -> None:
        case = self.case
        self.primary = case.equip_id
        self.downstream = case.downstream_path[-1]
        equipment = self.plant.equipment_by_id()
        process = {(a, b) for a, b, _, stream in self.plant.flow.edges if stream == "process"}
        if (case.plant_id != self.plant.plant.plant_id or equipment[self.primary].unit_id != case.unit_id
                or case.downstream_path[0] != self.primary
                or not set(zip(case.downstream_path, case.downstream_path[1:])) <= process):
            raise ValueError("물리 회로의 공장·Unit·하류 경로가 마스터와 일치해야 합니다.")
        branch = next((b for b in case.network.branches if b.branch_id == case.downstream_branch), None)
        if branch is None or branch.booster is None or case.control_valve not in branch.valves:
            raise ValueError("후속 펌프와 제어밸브는 선택한 제품 분기에 있어야 합니다.")

        self.keys: dict[str, dict[str, TagKey]] = {}
        for eid in (self.primary, self.downstream):
            signals = SIGNALS.keys() if eid == self.primary else SIGNALS.keys() - {"suction_pressure"}
            tags = [t for t in equipment[eid].tags if t.signal in signals]
            if len(tags) != len(signals) or {t.signal: t.unit for t in tags} != {s: SIGNALS[s] for s in signals}:
                raise ValueError(f"{eid}: 물리 계측 신호와 단위가 마스터와 일치해야 합니다.")
            self.keys[eid] = {t.signal: (eid, t.tag_id) for t in tags}
        self.tag_keys = {key for keys in self.keys.values() for key in keys.values()}
        self._solve_cached = lru_cache(maxsize=256)(self._solve)

    def tag_plan(self, mode: FaultMode, equip_id: str) -> dict[TagKey, TagPlan]:
        """기존 경험 태그는 유지하고, 물리 태그는 장부에 물리 적용으로 표시한다."""
        parameters = hydraulic_plan(mode) if equip_id == self.primary else {}
        result = {key: tp for key, tp in plan(self.plant, mode, equip_id, self.control).items()
                  if key not in self.tag_keys and (not parameters or tp.via != "process")}
        if parameters:
            applied = tuple(change for tp in parameters.values() for change in tp.applied)
            result.update({key: TagPlan(applied, (), via="physics") for key in sorted(self.tag_keys)})
        return result

    def parameter_plans(self, events: list[FaultEvent]) -> ParameterPlans:
        return [(event, params) for event in events if event.equip_id == self.primary
                and (params := hydraulic_plan(event.mode))]

    def state(self, epoch: int, plans: ParameterPlans) -> NetworkResult:
        amounts: dict[HydraulicParameter, float] = {}
        for event, parameters in plans:
            for name, tp in parameters.items():
                # 사건이 겹쳐도 검증한 동일 파라미터의 최대 이상 크기를 넘기지 않는다.
                amount, _ = event_tag_effect(tp, event, epoch)
                amounts[name] = max(amounts.get(name, 0.0), min(1.0, amount))
        return self.solve(amounts.get("strainer_blockage", 0.0), amounts.get("impeller_wear", 0.0),
                          amounts.get("valve_restriction", 0.0))

    def solve(self, clog: float, wear: float, restriction: float) -> NetworkResult:
        return self._solve_cached(clog, wear, restriction)

    def _solve(self, clog: float, wear: float, restriction: float) -> NetworkResult:
        case = self.case
        pump = case.network.pump
        head_factor = 1 - wear * case.max_impeller_head_loss
        point = case.default_point
        opening = point.valve_openings.get(case.control_valve, 1.0)
        network = case.network.model_copy(update={"pump": pump.model_copy(update={
            "shutoff_head_m": pump.shutoff_head_m * head_factor,
            "rated_head_m": pump.rated_head_m * head_factor,
            "efficiency": pump.efficiency * (1 - wear * case.max_impeller_efficiency_loss),
        })})
        return network.solve(point.model_copy(update={"valve_openings": {
            **point.valve_openings, case.control_valve: opening * (1 - restriction * case.max_valve_opening_loss),
        }}), clog * case.max_strainer_resistance_pa_s2_m6)

    def sensor_centers(self, state: NetworkResult) -> dict[TagKey, float]:
        normal = self.solve(0.0, 0.0, 0.0)
        product = next(b for b in state.branches if b.branch_id == self.case.downstream_branch)
        ref_product = next(b for b in normal.branches if b.branch_id == self.case.downstream_branch)
        source, downstream = self.keys[self.primary], self.keys[self.downstream]
        atm = self.case.atmospheric_pa
        return {
            source["suction_pressure"]: (state.suction_abs_pa - atm) / KGF_CM2_PA,
            source["discharge_pressure"]: (state.discharge_abs_pa - atm) / KGF_CM2_PA,
            source["flow"]: state.pump_flow_m3_s * 3600,
            source["current"]: self.control[source["current"]][0] * state.shaft_w / normal.shaft_w,
            downstream["discharge_pressure"]: (product.booster_discharge_abs_pa - atm) / KGF_CM2_PA,
            downstream["flow"]: product.flow_m3_s * 3600,
            downstream["current"]: self.control[downstream["current"]][0] * product.booster_shaft_w / ref_product.booster_shaft_w,
        }
