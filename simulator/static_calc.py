"""시뮬레이션 전용 정적 계산 (PU-M1). 수집기·SQLite·Redis·AE 없이 한 요청의 변경 목록을 매번 정상 운전점에서 다시 계산한다.

무상태: 요청마다 정상 운전점에서 시작하고 이전 요청을 기억하지 않는다. 잡음이 없어 같은 입력은 같은 결과를 낸다.
엔진은 다시 쓰지 않는다. 회로 해는 physics.station / physics_single, 고장 효과는 fault.plan·PhysicsBundle.tag_plan(트윈 모드),
교호작용 배율은 fault.boosted·factor_gain, 운전점 상사 법칙은 PlantConfig.control_by_key, 계산 태그는 Derive.value 를 그대로 쓴다.

정적 대표값(이 모듈의 정의, docs/physics-upgrade/m1-simulation-api.md)
- 고장은 단계를 고른다(기본: 마지막 단계). 그 단계까지의 step·ramp 효과가 끝까지 진행한 상태를 대표값으로 쓴다.
- spike·wobble(충격·맥동)은 시간 현상이라 정적 대표값을 정하지 않는다. 그런 효과가 있는 모드는 unsupported 다.
- 물리 파라미터 크기는 고른 단계까지 hydraulic amount 의 합(최대 1), 엔진 장부(state)와 같은 규칙이다.
- 공정 흐름 하류 경험 전파(process)는 트윈과 같이 계산하지 않는다. 그 태그는 not_computed 로 구분한다.
- 시간 지연·회복·재고(D6)와 센서 역산(D12)은 하지 않는다.
"""
from __future__ import annotations

import hashlib
import math
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from common.config_models import Operating, PlantConfig, TagKey, load_plant, load_yaml
from simulator.fault import HELD, FaultMode, TagPlan, boosted, factor_gain, hydraulic_plan, load_fault_modes, plan
from simulator.physics.npsh import collapse_extrapolation
from simulator.physics.pump import GRAVITY_M_S2
from simulator.physics.station import SolveError, Stopped, StationResult, UnitState
from simulator.physics_adapter import booster_strainer_centers, drive_centers, header_centers, redundant_centers, strainer_centers, PhysicsAdapter, PhysicsBundle, _outflow, load_physics
from simulator.physics_single import SingleAdapter

PLANTS = ("cdu", "hou", "rfcc", "rhds", "vrds")
ENGINE = "static-v1"
EPS = 1e-9
LEVELS = (("trip_hi", "trip"), ("trip_lo", "trip"), ("alarm_hi", "alarm"), ("alarm_lo", "alarm"),
          ("warn_hi", "warn"), ("warn_lo", "warn"))
RANK = {"normal": 0, "warn": 1, "alarm": 2, "trip": 3}
TRANSIENT = ("spike", "wobble")

# 원인 값 기본 범위 (synthetic assumption). 시작할 때 양 끝에서 해가 있는지 확인하고 없으면 정상 쪽으로 줄인다.
SPEED_RANGE = (0.85, 1.05)              # 회로 펌프 회전수 비 (D4 검사 0.85·0.95·1.05)
DRIVER_SPEED_RANGE = (0.9, 1.05)        # 회로 밖 구동기 회전수 비 (상사 법칙 경험 효과)
VALVE_RANGE = (0.1, 1.0)                # 입력 펌프 토출 차단밸브 개도 (D4 검사 10 %)
# 유량 제어 가정(2026-11-26 사용자 승인, 계산 전용 synthetic): 회로마다 가상 제어밸브가 정상 개도 70 %에서 동적 손실(배관 마찰+국부)의
# 1/3 을 맡는다고 보고, 제어기가 개도를 움직여 회로 총유량을 정상값에 맞춘다. 동적 손실 배율 m(a) = 2/3 + 1/3·(0.7/a)², 정상 m = 1.
# 개도 범위 10 %~100 % → m 0.83~17. 밸브가 끝에 닿으면(포화) 유량이 변한다. 정상 운전점 값은 바뀌지 않는다.
FIC_OPENING, FIC_SHARE, FIC_MIN_OPENING = 0.7, 1 / 3, 0.1


def _fic_m(opening: float) -> float:
    return (1 - FIC_SHARE) + FIC_SHARE * (FIC_OPENING / opening) ** 2


def _fic_opening(m: float) -> float:
    return FIC_OPENING / math.sqrt((m - (1 - FIC_SHARE)) / FIC_SHARE)


FIC_RANGE = (_fic_m(1.0), _fic_m(FIC_MIN_OPENING))
_FIC_OFF: ContextVar[bool] = ContextVar("fic_off", default=False)


@contextmanager
def flow_control_off():
    """이 안의 정적 계산은 유량 제어 가정 없이 개방 회로로 푼다. 자체 제어·액위 모델이 있는 D6 시간 재생이 쓴다."""
    token = _FIC_OFF.set(True)
    try:
        yield
    finally:
        _FIC_OFF.reset(token)
BLOCK_VALVE_MIN = 0.01                  # 고장 PM-DISCHARGE-BLOCK-VALVE 크기 1 의 개도(검증 하한 0.1 아래는 외삽, 2026-11-26)
BOUNDARY_SPAN = 0.2                     # 흡입 경계압 ± 게이지 정상값의 20 %
STOP_RESISTANCE_RANGE = (0.01, 0.3)     # 직렬 정지 pass·bypass 저항 = 단 정격 양정 대비 비율 (D4 민감도 2·10·30 %, 근거 없음)
SHRINK = (1.0, 0.75, 0.5, 0.25)
# 심각 범위 (M2 시뮬레이션 전용 외삽, 2026-11-26 사용자 승인). 위 범위는 '검증 범위'로 남기고, 그 밖은 결과에 외삽으로 표시한다.
# 막힘·마모·온도 상승 1 = 보정에서 정한 검증 최대(그 펌프 자신의 경보·캐비테이션 모델 한계). 1 을 넘으면 같은 배율로 더 키운다.
SEVERE = {"speed_ratio": (0.3, None), "discharge_valve_opening": (0.002, None), "control_valve_opening": (0.05, None),
          "strainer_blockage": (None, 8.0), "impeller_wear": (None, 1.5), "suction_temperature_rise": (None, 2.0)}
# 마모 ×1.5 (2026-11-26 사용자 결정, ×3 에서 축소): 양정 −15 %·효율 −30 % 정도. ×3 은 효율 −60 % 로 전류가 +52~+106 % 라 비현실적이었다.
SEVERE_SUCTION_LOW = 0.7                 # 흡입 경계압 하한: 게이지 정상값의 −70 %
MAX_ITEMS = 64                           # 목록별 최대 항목 수 (요청 크기 제한)

ROOT = Path(__file__).resolve().parent.parent


def _machine(head_m: float | None, shaft_w: float | None, npsh_margin: float | None = None) -> dict:
    """펌프 단위 기계 값(D11): 양정(m)과 축동력(kW). 계산할 수 없으면(체절 등) 넣지 않는다. 전류는 태그(II-*) 쪽에 있다.
    D12: 흡입 여유 NPSHa/NPSHr(흡입 모델이 있는 펌프만)."""
    return {**({"head_m": head_m} if head_m is not None else {}), **({"shaft_kw": shaft_w / 1000} if shaft_w is not None else {}),
            **({"npsh_margin": npsh_margin} if npsh_margin is not None else {})}


class CalcError(Exception):
    """계산하지 못한 이유. code: input_error · out_of_range · conflict · unsupported · no_solution · not_converged · engine_error."""

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail

    def body(self) -> dict:
        return {"code": self.code, "message": self.message, **({"detail": self.detail} if self.detail else {})}


def stage_of(value: float, limits) -> str:
    """설비 자신의 한계값으로 독립 판정 (trip → alarm → warn, 엄격 비교). 한계값은 바꾸지 않는다."""
    if limits is None:
        return "normal"
    for name, level in LEVELS:
        bound = getattr(limits, name)
        if bound is not None and (value > bound if name.endswith("_hi") else value < bound):
            return level
    return "normal"


def _solve_error(exc: Exception) -> CalcError:
    """엔진 오류 → 응답 오류. 값을 대신 만들지 않는다."""
    reason = exc.reason if isinstance(exc, SolveError) else (
        "cavitation_range" if "유효 범위" in str(exc) else "dry_run" if "양수여야" in str(exc)
        else "no_solution" if "순방향 유량" in str(exc) else "input")
    if reason == "not_converged":
        return CalcError("not_converged", str(exc))
    if reason == "input":
        return CalcError("engine_error", f"엔진이 입력을 거절했습니다: {exc}")
    return CalcError("no_solution", str(exc), reason=reason)


@dataclass(frozen=True)
class Param:
    id: str
    owner: str              # 설비 ID 또는 회로 ID
    circuit: str | None
    name: str
    unit: str
    normal: float
    lo: float
    hi: float
    basis: str              # physics / empirical
    label: str
    note: str | None = None
    vlo: float | None = None    # 검증 범위 (None 이면 lo·hi 와 같다). lo·hi 는 심각 범위까지 넓힌 값
    vhi: float | None = None

    def validated(self) -> tuple[float, float]:
        return (self.lo if self.vlo is None else self.vlo, self.hi if self.vhi is None else self.vhi)

    def extrapolated(self, value: float) -> bool:
        lo, hi = self.validated()
        return not lo - EPS <= value <= hi + EPS

    def public(self) -> dict:
        lo, hi = self.validated()
        extra = (f"검증 범위 {lo:g}~{hi:g}. 그 밖은 심각 구간 외삽입니다(캐비테이션 붕괴 등, 검증하지 않음)."
                 if (lo, hi) != (self.lo, self.hi) else None)
        note = " ".join(x for x in (self.note, extra) if x) or None
        return {"id": self.id, "target": self.owner, "circuit_id": self.circuit, "parameter": self.name, "unit": self.unit,
                "normal": self.normal, "min": self.lo, "max": self.hi, "basis": self.basis, "name": self.label, "note": note}


@dataclass
class _Inputs:
    """한 회로에 들어가는 변경 (요청마다 새로 만든다)."""
    amounts: dict[tuple[str, str], float] = field(default_factory=dict)     # (설비, 물리 파라미터) → 0~1
    values: dict[str, float] = field(default_factory=dict)                  # 원인 파라미터 이름(owner/name) → 값
    stops: dict[str, Stopped | None] = field(default_factory=dict)          # 정지한 입력 펌프 → 직렬 처리 (병렬 None)

    def empty(self) -> bool:
        return not (self.amounts or self.values or self.stops)


class PlantCalc:
    """한 공장의 정적 계산기. 시작할 때 설정을 읽고 정상 해와 원인 값 범위를 확인한다. 요청 사이에 상태를 두지 않는다."""

    def __init__(self, key: str, config: Path = ROOT / "config", flow_control: bool = True) -> None:
        self.key = key
        self.flow_control = flow_control      # False: 제어기 없는 개방 회로(엔진 해 대조 검사용)
        paths = [config / f"plant_{key}.yaml", config / f"physics_{key}_twin.yaml", config / "fault_modes.yaml"]
        self.plant: PlantConfig = load_plant(paths[0])
        self.physics: PhysicsBundle = load_physics(self.plant, paths[1])      # 트윈 모드(general=False)
        self.modes: dict[str, FaultMode] = load_fault_modes(paths[2])
        circuit_files = [paths[1].parent / f for f in load_yaml(paths[1])["circuits"]]
        # 정적 계산 전용 단일 회로(M2 하류 전파): 트윈 묶음에서 제외한 펌프. 수집기 트윈은 이 파일을 읽지 않는다.
        extra = config / f"physics_{key}_calc.yaml"
        extra_cfg = load_yaml(extra) if extra.exists() else {"circuits": [], "notes": {}}
        self.extra = [SingleAdapter(self.plant, config / f) for f in extra_cfg["circuits"]]
        self.extra_notes: dict[str, str] = extra_cfg.get("notes") or {}
        self._extra_by = {a.primary: a for a in self.extra}
        # 정적 계산 전용 고장 적용 확장(물리 파라미터가 있는 설비만). 수집기 트윈·AE 데이터의 fault_modes.yaml 은 바꾸지 않는다.
        self.fault_targets: dict[str, set[str]] = {m: set(ids) for m, ids in (extra_cfg.get("faults") or {}).items()}
        calc_modes = config / "fault_modes_calc.yaml"
        if calc_modes.exists():
            self.modes.update(load_fault_modes(calc_modes))
            paths.append(calc_modes)
        unknown = set(self.fault_targets) - set(self.modes)
        if unknown:
            raise ValueError(f"physics_{key}_calc.yaml faults: 알 수 없는 고장 모드 {sorted(unknown)}")
        if extra.exists():
            paths.append(extra)
            circuit_files += [config / f for f in extra_cfg["circuits"]]
        digest = hashlib.sha256()
        for p in paths + circuit_files:
            digest.update(p.read_bytes())
        self.model_version = {"engine": ENGINE, "config_version": self.plant.plant.config_version,
                              "config_sha256": digest.hexdigest()[:16]}
        self.by_id = self.plant.equipment_by_id()
        self.tags = self.plant.tag_by_key()
        self.control = self.plant.control_by_key()
        self.order = [(e.equip_id, t.tag_id) for e in self.plant.equipment for t in e.tags]
        self.driver_speed: dict[str, TagKey] = {}
        for e in self.plant.equipment:
            if e.driven_by:
                d = self.by_id[e.driven_by]
                ds = next((t for t in d.tags if t.signal == "speed"), None)
                own = next((t for t in e.tags if t.signal == "speed"), None)
                if ds and own:
                    self.driver_speed[e.equip_id] = (d.equip_id, ds.tag_id)
        self.roles = {**self.physics.roles, **{a.primary: "single" for a in self.extra}}
        self.circuit_of = {**{eid: self.physics.circuit_of(eid) for eid in self.physics.roles},
                           **{a.primary: a.circuit_id for a in self.extra}}
        self.adapters = {a.circuits()[0]["id"]: a for a in [*self.physics.adapters, *self.extra]}
        # 정상 해는 시작할 때 한 번 풀고 읽기만 한다 (요청 사이 공유, 쓰기 없음)
        self._ref_station: dict[str, StationResult] = {}
        for a in self.adapters.values():
            if isinstance(a, PhysicsAdapter):
                station, point, states, booster_r = a.case.scenario()
                self._ref_station[a.primary] = station.solve(point, states, booster_suction_resistance=booster_r)
        normal = {cid: self._centers(cid, _Inputs()) for cid in self.adapters}
        self._normal_centers = {cid: c for cid, (c, _) in normal.items()}
        self._normal_flow = {cid: bal["total_flow_m3_h"] for cid, (_, bal) in normal.items()}
        self.split_from = self._split_sources()
        self.speed_tag = {e.equip_id: (e.equip_id, t.tag_id) for e in self.plant.equipment for t in e.tags if t.signal == "speed"}
        self.normal_values: dict[TagKey, float] = self._calculate({}, baseline=True)["_values"]
        self.params = {k: self._explained(p) for k, p in self._params().items()}

    # --- 원인 파라미터 ---------------------------------------------------------------------
    MULTIPLE_UNIT = "배수(1 = 검증 최대)"

    def _explained(self, p: Param) -> Param:
        """0~1 로 정의한 고장 크기 파라미터(막힘·마모·흡입 온도)의 단위를 '배수'로 바꾸고, 1 의 물리적 뜻을 note 앞에 붙인다.
        뜻의 숫자는 값에 비례한다(막힘 저항·양정·효율 감소·온도 상승 모두 선형). 화면이 현재 값으로 환산해 보인다."""
        if p.name not in ("strainer_blockage", "impeller_wear", "suction_temperature_rise"):
            return p
        a = self.adapters[p.circuit]
        case = a.case
        if p.name == "suction_temperature_rise":
            text = f"1 = 흡입 유체 온도 +{case.max_temperature_rise_k:g} K"
            label = p.label.split("(")[0]
        elif p.name == "impeller_wear":
            booster = isinstance(a, PhysicsAdapter) and p.owner == a.downstream
            head = case.max_booster_head_loss if booster else case.max_impeller_head_loss
            eff = case.max_booster_efficiency_loss if booster else case.max_impeller_efficiency_loss
            text, label = f"1 = 양정 −{head * 100:g} %·효율 −{eff * 100:g} %", p.label
        else:
            # 막힘은 비선형이고, 흡입압 태그가 없는 펌프(후속·2단)는 최대 저항을 유량 경보에서 역산해 '정상 유량 손실'이 뜻이 없다.
            # 그래서 1 의 뜻은 이 값만 1 로 바꾼 실제 계산 결과(유량 제어 가정 포함)로 적는다. 화면은 비례 환산하지 않는다.
            x = _Inputs()
            x.amounts[(p.owner, p.name)] = 1.0
            keys = a.keys[p.owner] if isinstance(a, PhysicsAdapter) else a.keys
            try:
                c, _ = self._centers(p.circuit, x)
            except CalcError:
                c = {}
            n = self._normal_centers[p.circuit]
            parts = []
            fk = keys.get("flow")
            if fk in c and c[fk] is not None and n.get(fk):
                parts.append(f"유량 {100 * (c[fk] - n[fk]) / n[fk]:+.1f} %")
            sk = keys.get("suction_pressure")
            if sk in c and c[sk] is not None:
                parts.append(f"흡입압 {c[sk] - n[sk]:+.3g} {self.tags[sk].unit}")
            text = "1 = 이 값만 바꾸면 " + ("·".join(parts) if parts else "계산 불가")
            label = p.label
        return replace(p, unit=self.MULTIPLE_UNIT, label=label, note=f"{text}." + (f" {p.note}" if p.note else ""))

    def _pressure_unit(self, key: TagKey) -> tuple[str, float]:
        unit = self.tags[key].unit
        return unit, {"kg/cm2(g)": 98_066.5, "bar(g)": 100_000.0}[unit]

    def _params(self) -> dict[str, Param]:
        out: list[Param] = []
        for cid, a in self.adapters.items():
            if isinstance(a, PhysicsAdapter):
                case = a.case
                key = a.keys[a.primary]["suction_pressure"]
                unit, pa = self._pressure_unit(key)
                gauge = (case.default_point.suction_abs_pa - case.atmospheric_pa) / pa
                out.append(Param(f"{cid}/suction_pressure", cid, cid, "suction_pressure", unit, gauge,
                                 gauge * (1 - BOUNDARY_SPAN), gauge * (1 + BOUNDARY_SPAN), "physics", "흡입 경계압"))
                out.append(Param(f"{cid}/control_valve_opening", cid, cid, "control_valve_opening", "ratio", 1.0,
                                 1 - case.max_valve_opening_loss, 1.0, "physics", "제품 분기 제어밸브 개도(정상 대비)"))
                for u in (a.primary, a.second):
                    out += [Param(f"{u}/speed_ratio", u, cid, "speed_ratio", "ratio", 1.0, *SPEED_RANGE, "physics", "회전수 비"),
                            Param(f"{u}/strainer_blockage", u, cid, "strainer_blockage", "fraction", 0.0, 0.0, 1.0, "physics", "흡입 스트레이너 막힘"),
                            Param(f"{u}/impeller_wear", u, cid, "impeller_wear", "fraction", 0.0, 0.0, 1.0, "physics", "임펠러 마모"),
                            Param(f"{u}/discharge_valve_opening", u, cid, "discharge_valve_opening", "ratio", 1.0, *VALVE_RANGE, "physics", "토출 차단밸브 개도")]
                d = a.downstream
                out += [Param(f"{d}/speed_ratio", d, cid, "speed_ratio", "ratio", 1.0, *SPEED_RANGE, "physics", "회전수 비"),
                        Param(f"{d}/strainer_blockage", d, cid, "strainer_blockage", "fraction", 0.0, 0.0, 1.0, "physics", "흡입 스트레이너 막힘"),
                        Param(f"{d}/impeller_wear", d, cid, "impeller_wear", "fraction", 0.0, 0.0, 1.0, "physics", "임펠러 마모")]
            else:
                e, case = a.primary, a.case
                out.append(Param(f"{e}/speed_ratio", e, cid, "speed_ratio", "ratio", 1.0, *SPEED_RANGE, "physics", "회전수 비"))
                if "suction_pressure" in a.keys:
                    unit, pa = self._pressure_unit(a.keys["suction_pressure"])
                    gauge = (case.source_abs_pa - case.atmospheric_pa) / pa
                    out.append(Param(f"{cid}/suction_pressure", cid, cid, "suction_pressure", unit, gauge,
                                     gauge * (1 - BOUNDARY_SPAN), gauge * (1 + BOUNDARY_SPAN), "physics", "흡입 경계압"))
                out += [Param(f"{e}/strainer_blockage", e, cid, "strainer_blockage", "fraction", 0.0, 0.0, 1.0, "physics", "흡입 막힘"),
                        Param(f"{e}/impeller_wear", e, cid, "impeller_wear", "fraction", 0.0, 0.0, 1.0, "physics", "임펠러 마모·오염")]
                if "suction_temperature_rise" in a.parameters:
                    out.append(Param(f"{e}/suction_temperature_rise", e, cid, "suction_temperature_rise", "fraction", 0.0, 0.0, 1.0,
                                     "physics", f"흡입 유체 온도 상승(1 = +{case.max_temperature_rise_k:g} K)"))
        for e in self.plant.equipment:
            speed = next((t for t in e.tags if t.signal == "speed"), None)
            if speed and not e.driven_by and e.equip_id not in self.physics.roles:
                out.append(Param(f"{e.equip_id}/speed_ratio", e.equip_id, None, "speed_ratio", "ratio", 1.0, *DRIVER_SPEED_RANGE,
                                 "empirical", "구동기 회전수 비",
                                 "물리 회로 밖: 운전점 상사 법칙(회전수¹·유량¹·전류³·진동²)으로 이 설비와 축 사슬의 기준값만 옮긴다."))
        return {p.id: self._checked(self._severe(p)) for p in out}

    def _severe(self, p: Param) -> Param:
        """검증 범위를 vlo·vhi 로 남기고 lo·hi 를 심각 범위로 넓힌다. 회로 밖 경험 파라미터는 넓히지 않는다."""
        if p.circuit is None:
            return p
        lo, hi = p.lo, p.hi
        if p.name == "suction_pressure":
            lo = min(lo, p.normal * (1 - SEVERE_SUCTION_LOW))
        elif p.name in SEVERE:
            slo, shi = SEVERE[p.name]
            lo = lo if slo is None else min(lo, slo)
            hi = hi if shi is None else max(hi, shi)
            if p.name == "impeller_wear":                 # 양정·효율 감소가 1 미만으로 남는 배율까지
                hi = min(hi, self._wear_cap(p))
        if (lo, hi) == (p.lo, p.hi):
            return p
        return replace(p, lo=lo, hi=hi, vlo=p.lo, vhi=p.hi)

    def _wear_cap(self, p: Param) -> float:
        a = self.adapters[p.circuit]
        case = a.case
        if isinstance(a, PhysicsAdapter):
            losses = ([case.max_booster_head_loss, case.max_booster_efficiency_loss] if p.owner == a.downstream
                      else [case.max_impeller_head_loss, case.max_impeller_efficiency_loss])
        else:
            losses = [case.max_impeller_head_loss, case.max_impeller_efficiency_loss]
        return min(SEVERE["impeller_wear"][1], 0.9 / max(losses))

    def _solvable(self, p: Param, v: float) -> bool:
        try:
            self._centers(p.circuit, self._inputs_for(p, v))
            return True
        except CalcError:
            return False

    def _checked(self, p: Param) -> Param:
        """양 끝에서 엔진 해가 없으면 정상 쪽으로 줄인다. 경험 파라미터는 엔진 풀이가 없다.
        심각 범위가 있으면 검증 끝과 심각 끝 사이를 이분법으로 찾아 해가 있는 가장 먼 값을 쓴다(검증 끝보다 줄이지 않는다)."""
        if p.circuit is None:
            return p
        if p.vlo is not None:
            ends = []
            for v_end, s_end in ((p.vlo, p.lo), (p.vhi, p.hi)):
                if v_end == s_end or self._solvable(p, s_end):
                    ends.append(s_end)
                    continue
                if not self._solvable(p, v_end):
                    ends = None
                    break
                good, bad = v_end, s_end
                for _ in range(12):
                    mid = (good + bad) / 2
                    good, bad = (mid, bad) if self._solvable(p, mid) else (good, mid)
                ends.append(float(f"{good:.4g}") if self._solvable(p, float(f"{good:.4g}")) else good)
            if ends is not None:
                lo, hi = ends
                if (lo, hi) == (p.lo, p.hi):
                    return p
                note = f"엔진 해가 있는 심각 범위로 줄였습니다(요청 {p.lo:g}~{p.hi:g})."
                return replace(p, lo=lo, hi=hi, note=note if p.note is None else f"{p.note} {note}")
            p = replace(p, lo=p.vlo, hi=p.vhi, vlo=None, vhi=None)
        bounds = []
        for end in (p.lo, p.hi):
            for f in SHRINK:
                v = end if f == 1.0 else p.normal + (end - p.normal) * f
                try:
                    self._centers(p.circuit, self._inputs_for(p, v))
                    bounds.append(v)
                    break
                except CalcError:
                    continue
            else:
                bounds.append(p.normal)
        lo, hi = bounds
        if (lo, hi) == (p.lo, p.hi):
            return p
        note = f"엔진 해가 있는 범위로 줄였습니다(요청 범위 {p.lo:g}~{p.hi:g})."
        return replace(p, lo=lo, hi=hi, note=note if p.note is None else f"{p.note} {note}")

    def _inputs_for(self, p: Param, value: float) -> _Inputs:
        x = _Inputs()
        if p.name in ("strainer_blockage", "impeller_wear", "suction_temperature_rise"):
            x.amounts[(p.owner, p.name)] = value
        else:
            x.values[p.id] = value
        return x

    # --- 회로 풀이 ---------------------------------------------------------------------------
    def _centers(self, cid: str, x: _Inputs) -> tuple[dict[TagKey, float | None], dict]:
        a = self.adapters[cid]
        with collapse_extrapolation():
            return self._coupled(a, x) if isinstance(a, PhysicsAdapter) else self._single(a, x)

    def _coupled(self, a: PhysicsAdapter, x: _Inputs) -> tuple[dict[TagKey, float | None], dict]:
        case, cid = a.case, a.circuit_of(a.primary)
        amt = lambda e, n: x.amounts.get((e, n), 0.0)       # noqa: E731  (고장 양은 1 이하, 원인 값은 심각 범위까지)
        restriction = amt(a.primary, "valve_restriction")
        if f"{cid}/control_valve_opening" in x.values:
            restriction = (1 - x.values[f"{cid}/control_valve_opening"]) / case.max_valve_opening_loss
        station, point, states, booster_r = case.scenario(
            amt(a.primary, "strainer_blockage"), amt(a.primary, "impeller_wear"), restriction,
            amt(a.downstream, "strainer_blockage"), amt(a.downstream, "impeller_wear"),
            amt(a.second, "strainer_blockage"), amt(a.second, "impeller_wear"))
        if f"{cid}/suction_pressure" in x.values:
            unit, pa = self._pressure_unit(a.keys[a.primary]["suction_pressure"])
            point = point.model_copy(update={"suction_abs_pa": case.atmospheric_pa + x.values[f"{cid}/suction_pressure"] * pa})
        for u in (a.primary, a.second):
            upd = {}
            if f"{u}/speed_ratio" in x.values:
                upd["speed_ratio"] = x.values[f"{u}/speed_ratio"]
            if f"{u}/discharge_valve_opening" in x.values:
                upd["valve_opening"] = x.values[f"{u}/discharge_valve_opening"]
            if u in x.stops:
                upd.update(running=False, stopped=x.stops[u])
            if upd:
                states[u] = UnitState.model_validate({**states.get(u, UnitState()).model_dump(), **upd})
        ratio = x.values.get(f"{a.downstream}/speed_ratio")
        if ratio is not None:
            branches = [b.model_copy(update={"booster": b.booster.model_copy(update={"speed_rpm": b.booster.speed_rpm * ratio})})
                        if b.branch_id == case.downstream_branch else b for b in station.demand.branches]
            station = station.model_copy(update={"demand": station.demand.model_copy(update={"branches": branches})})
        control = None
        q0 = self.__dict__.get("_normal_flow", {}).get(cid)
        manual = restriction > 0 or f"{cid}/control_valve_opening" in x.values   # 밸브를 사람이 정했거나 밸브 고장이면 제어기 수동
        if q0 is not None and not manual and self.flow_control and not _FIC_OFF.get():
            station, control = self._fic_branches(station, point, states, booster_r, self._ref_station[a.primary])
        try:
            r = station.solve(point, states, booster_suction_resistance=booster_r)
        except SolveError as exc:
            raise _solve_error(exc) from exc
        except ValueError as exc:     # pydantic 검증(범위 밖 상태) 포함
            raise CalcError("engine_error", f"엔진이 입력을 거절했습니다: {exc}") from exc
        collapse = [u.unit_id for u, cu in zip(r.units, case.station.units)
                    if cu.npsh is not None and (u.cavitation_head_loss or 0) >= cu.npsh.max_head_loss]
        booster = next((b for b in station.demand.branches if b.branch_id == case.downstream_branch), None)
        product = next(b for b in r.branches if b.branch_id == case.downstream_branch)
        if booster is not None and booster.booster is not None and booster.booster.npsh is not None \
                and (product.booster_head_loss or 0) >= booster.booster.npsh.max_head_loss:
            collapse.append(a.downstream)
        return self._coupled_centers(a, r, point.valve_openings.get(case.control_valve, 1.0)), {
            **({"_collapse": collapse} if collapse else {}), **({"_control": control} if control else {}),
            **({"_drum": {"equip_id": a.downstream, "state": product.drum_state, "opening": product.level_valve_opening}}
               if product.drum_state not in (None, "steady") else {}),
            "circuit_id": cid, "mass_residual_kg_s": r.mass_residual_kg_s,
            "pressure_residual_pa": max([abs(u.pressure_residual_pa) for u in r.units] + [abs(b.pressure_residual_pa) for b in r.branches]),
            "energy_residual_w": r.energy_residual_w, "total_flow_m3_h": r.flow_m3_s * 3600,
            "header_kpa_abs": r.header_abs_pa / 1000,
            "units": [{"equip_id": u.unit_id, "status": u.status, "flow_m3_h": u.flow_m3_s * 3600,
                       **_machine(u.head_m, u.shaft_w, u.npsh_margin)} for u in r.units]
                     + ([{"equip_id": a.downstream, "status": "running", "role": "downstream",
                          "flow_m3_h": (product.booster_flow_m3_s if product.booster_flow_m3_s is not None else product.flow_m3_s) * 3600,
                          **_machine((product.booster_discharge_abs_pa - product.booster_suction_abs_pa) / (station.demand.density_kg_m3 * GRAVITY_M_S2),
                                     product.booster_shaft_w, product.booster_npsh_margin)}]
                        if product.booster_shaft_w is not None and product.booster_discharge_abs_pa is not None else [])}

    def _coupled_centers(self, a: PhysicsAdapter, r: StationResult, opening: float | None = None) -> dict[TagKey, float | None]:
        """PhysicsAdapter.sensor_centers 와 같은 환산. 정지 펌프도 다루려고 StationResult 를 직접 읽는다.
        전류는 정상 축동력 비(엔진과 같은 정의). 체절 운전(dead_headed)은 엔진이 축동력을 계산하지 않아 None."""
        normal = self._ref_station[a.primary]
        product = next(b for b in r.branches if b.branch_id == a.case.downstream_branch)
        ref_product = next(b for b in normal.branches if b.branch_id == a.case.downstream_branch)
        d = a.keys[a.downstream]
        out: dict[TagKey, float | None] = {
            d["discharge_pressure"]: a._gauge(d["discharge_pressure"], product.booster_discharge_abs_pa),
            d["flow"]: _outflow(product) * 3600}
        for u, u0 in zip(r.units, normal.units):
            k = a.keys[u.unit_id]
            out.update({k["suction_pressure"]: a._gauge(k["suction_pressure"], u.suction_abs_pa),
                        k["discharge_pressure"]: a._gauge(k["discharge_pressure"], u.discharge_abs_pa),
                        k["flow"]: u.flow_m3_s * 3600,
                        k["current"]: None if u.shaft_w is None else self.control[k["current"]][0] * u.shaft_w / u0.shaft_w})
            out.update(strainer_centers(a, k, u))
            out.update(drive_centers(k, u.speed_rpm, opening))
        if "suction_pressure" in d:
            out[d["suction_pressure"]] = a._gauge(d["suction_pressure"], product.booster_suction_abs_pa)
        if "current" in d:
            out[d["current"]] = self.control[d["current"]][0] * product.booster_shaft_w / ref_product.booster_shaft_w
        out.update(booster_strainer_centers(a, d, product))
        out.update(drive_centers(d, product.booster_speed_rpm, opening))
        out.update(redundant_centers(a.keys.values(), out))
        out.update(header_centers(a.keys.values(), r.flow_m3_s))
        return out

    def _split_sources(self) -> dict[str, str]:
        """고정 분배 대용모델: 결합 회로 설비에서 공정(process) 간선으로 닿는 단일 펌프 회로 → 그 결합 회로 ID.
        구동 축(drives)·보조 계통(utility)은 따라가지 않는다. 결합 회로가 여럿이면 앞의 것."""
        out: dict[str, str] = {}
        edges: dict[str, list[str]] = {}
        for a, b, _, stream in self.plant.flow.edges:
            if stream == "process":
                edges.setdefault(a, []).append(b)
        for cid, ad in self.adapters.items():
            if not isinstance(ad, PhysicsAdapter):
                continue
            seen, stack = set(ad.roles), list(ad.roles)
            while stack:
                for b in edges.get(stack.pop(), []):
                    if b not in seen:
                        seen.add(b)
                        stack.append(b)
            for sid, sa in self.adapters.items():
                if isinstance(sa, SingleAdapter) and sa.case.kind == "pump" and sa.primary in seen:
                    out.setdefault(sid, cid)
        return out

    def _single(self, a: SingleAdapter, x: _Inputs, target_m3_h: float | None = None) -> tuple[dict[TagKey, float | None], dict]:
        e, cid = a.primary, a.circuit_id
        amt = lambda n: x.amounts.get((e, n), 0.0)      # noqa: E731
        case, strainer, temp = a.case.scenario(amt("strainer_blockage"), amt("impeller_wear"), amt("suction_temperature_rise"))
        upd = {}
        if f"{e}/speed_ratio" in x.values:
            upd["machine"] = case.machine.model_copy(update={"speed_rpm": case.machine.speed_rpm * x.values[f"{e}/speed_ratio"]})
        if f"{cid}/suction_pressure" in x.values:
            unit, pa = self._pressure_unit(a.keys["suction_pressure"])
            upd["source_abs_pa"] = case.atmospheric_pa + x.values[f"{cid}/suction_pressure"] * pa
        case = case.model_copy(update=upd) if upd else case
        reached, control = True, None
        q0 = self.__dict__.get("_normal_flow", {}).get(cid)
        if target_m3_h is None and q0 is not None and self.flow_control and not _FIC_OFF.get():       # 고정 분배 대상이 아니면 자기 유량 제어(정상 유량 유지)
            pipe = case.discharge_pipe
            at = lambda m: case.model_copy(update={"discharge_pipe": pipe.model_copy(update={      # noqa: E731
                "darcy_factor": pipe.darcy_factor * m, "minor_loss_k": pipe.minor_loss_k * m})})
            m, control = self._fic(lambda m: at(m).solve_point(strainer, temp).flow_m3_s, q0 / 3600)
            case = at(m)
        try:
            if target_m3_h is not None:
                case, reached = self._throttled(case, strainer, temp, target_m3_h / 3600)
            r = case.solve_point(strainer, temp)
        except ValueError as exc:
            raise _solve_error(exc) from exc
        # 증기압 이하(NPSHa ≤ 0)는 값을 내지 않는다. 양정 감소가 검증 상한을 넘으면 붕괴 외삽으로 표시한다.
        if case.npsh is not None and r.npsha_m <= 0:
            raise CalcError("no_solution", f"흡입압이 증기압 이하입니다: NPSHa {r.npsha_m:.2f} m", reason="cavitation_range")
        collapse = case.npsh is not None and r.head_loss >= case.npsh.max_head_loss
        return a.sensor_centers(r, calc_only=True), {"circuit_id": cid, "mass_residual_kg_s": 0.0, "pressure_residual_pa": abs(r.pressure_residual_pa),
                         "energy_residual_w": r.energy_residual_w, "total_flow_m3_h": r.flow_m3_s * 3600,
                         "units": [{"equip_id": e, "status": "running", "flow_m3_h": r.flow_m3_s * 3600,
                                    **_machine(r.rise_pa / (case.density_kg_m3 * GRAVITY_M_S2) if case.kind == "pump" else None, r.shaft_w,
                                             r.npsh_margin if case.npsh is not None else None)}],
                         **({} if reached else {"_unreached": True}), **({"_collapse": [e]} if collapse else {}),
                         **({"_control": control} if control else {})}

    @staticmethod
    def _scaled_station(station, ms: dict[str, float]):
        """유량 제어 가정: 분기마다 동적 손실(배관 마찰·국부, 밸브, 후속 펌프 토출 배관)을 m 배. 출구압·높이차는 그대로."""
        branches = []
        for b in station.demand.branches:
            m = ms.get(b.branch_id, 1.0)
            if m == 1.0:
                branches.append(b)
                continue
            pipe = lambda p, m=m: p.model_copy(update={"darcy_factor": p.darcy_factor * m, "minor_loss_k": p.minor_loss_k * m})  # noqa: E731
            upd = {"pipes": {k: pipe(p) for k, p in b.pipes.items()},
                   "valves": {k: v.model_copy(update={"full_open_k": v.full_open_k * m}) for k, v in b.valves.items()}}
            if b.booster is not None:
                upd["booster"] = b.booster.model_copy(update={"discharge_pipe": pipe(b.booster.discharge_pipe)})
            branches.append(b.model_copy(update=upd))
        return station.model_copy(update={"demand": station.demand.model_copy(update={"branches": branches})})

    def _fic_branches(self, station, point, states, booster_r, normal: StationResult):
        """분기마다 유량 제어기(제품 인출 FIC)가 자기 정상 유량을 지킨다. 분기 유량 ∝ 1/√m 근사로 m 을 고쳐 가며 맞춘다(감쇠 반복).
        반환 (제어를 반영한 station, 제어 정보 또는 None). 엔진이 풀지 못하면 제어 없이 둔다."""
        target = {b.branch_id: b.flow_m3_s for b in normal.branches}
        lo, hi = FIC_RANGE

        def solve(ms):
            try:
                return station.solve(point, states, booster_suction_resistance=booster_r) if not ms else \
                    self._scaled_station(station, ms).solve(point, states, booster_suction_resistance=booster_r)
            except (SolveError, ValueError):
                return None
        r = solve({})
        if r is not None and all(abs(b.flow_m3_s - target[b.branch_id]) <= 1e-7 * target[b.branch_id] for b in r.branches):
            return station, None
        ms = {k: 1.0 for k in target}
        last = r
        for _ in range(400):
            if last is None:
                return station, None
            err = max(abs(b.flow_m3_s / target[b.branch_id] - 1) for b in last.branches)
            if err < 1e-11:          # 상태 판정(EPS 1e-9)보다 작게: 제어가 지킨 유량을 '변함'으로 보이지 않게
                break
            new = {}
            for b in last.branches:
                ratio = max(b.flow_m3_s, 1e-9) / target[b.branch_id]
                new[b.branch_id] = min(hi, max(lo, ms[b.branch_id] * ratio ** 1.6))
            if new == ms:
                break                                   # 모두 끝에 닿아 더 움직일 수 없다
            ms = new
            last = solve(ms)
        if last is None:
            return station, None
        sat = any(abs(b.flow_m3_s / target[b.branch_id] - 1) > 1e-4 for b in last.branches)
        openings = [_fic_opening(m) for m in ms.values()]
        return self._scaled_station(station, ms), {"m": ms, "opening": max(openings), "opening_min": min(openings), "saturated": sat}

    @staticmethod
    def _opening_text(ctl: dict) -> str:
        lo, hi = round(ctl.get("opening_min", ctl["opening"]) * 100), round(ctl["opening"] * 100)
        return f"{hi} %" if lo == hi else f"{lo}~{hi} %(분기별)"

    @staticmethod
    def _fic(flow_at, target: float) -> tuple[float, dict | None]:
        """유량 제어기: 유량이 m 에 단조 감소한다고 보고 FIC_RANGE 안에서 target 을 맞추는 m. 반환 (m, 제어 정보 또는 None=그대로).
        엔진이 풀지 못하는 m 은 유량 0 으로 본다. 끝에 닿으면 포화(saturated)."""
        def q(m: float) -> float | None:
            try:
                return flow_at(m)
            except (SolveError, ValueError):
                return None
        q1 = q(1.0)
        if q1 is not None and abs(q1 - target) <= 1e-12 * target:
            return 1.0, None
        lo, hi = FIC_RANGE
        q_lo, q_hi = q(lo), q(hi)
        if q_lo is None:
            return 1.0, None
        if q_lo <= target:
            m, sat = lo, True
        elif q_hi is not None and q_hi >= target:
            m, sat = hi, True
        else:
            a, b = math.log(lo), math.log(hi)
            for _ in range(100):
                mid = (a + b) / 2
                qm = q(math.exp(mid))
                if qm is not None and qm > target:
                    a = mid
                else:
                    b = mid
                if abs(b - a) < 1e-15:
                    break
            m, sat = math.exp(a), False
        return m, {"m": m, "opening": _fic_opening(m), "saturated": sat}

    @staticmethod
    def _throttled(case, strainer: float, temp: float, target: float):
        """토출 제어밸브 가정: 토출 배관 국부 손실 계수 배율 s(≥0)를 골라 유량을 목표에 맞춘다. 유량은 s 에 단조 감소.
        밸브를 다 열어도(s = 0) 목표에 못 미치면 다 연 상태로 두고 reached=False. 반환: (밸브를 반영한 사본, reached)."""
        pipe = case.discharge_pipe
        at = lambda s: case.model_copy(update={"discharge_pipe": pipe.model_copy(update={"minor_loss_k": pipe.minor_loss_k * s})})  # noqa: E731

        def flow(s: float) -> float:
            try:
                return at(s).solve_point(strainer, temp).flow_m3_s
            except ValueError:
                return 0.0
        if flow(0.0) <= target:
            return at(0.0), False
        hi = 1.0
        while flow(hi) > target and hi < 1e6:
            hi *= 2
        lo = 0.0
        for _ in range(80):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if flow(mid) > target else (lo, mid)
        return at((lo + hi) / 2), True

    # --- 고장 카탈로그 ---------------------------------------------------------------------------
    def _applies(self, mode: FaultMode, equip_id: str) -> bool:
        return mode.applies_to(self.by_id[equip_id]) or equip_id in self.fault_targets.get(mode.id, ())

    def _physical(self, mode: FaultMode, equip_id: str) -> dict:
        """그 설비에서 이 모드가 움직이는 물리 파라미터. 계산 전용 단일 회로는 그 회로가, 결합 회로 입력 펌프의
        토출 차단밸브(discharge_valve_restriction)는 정적 계산이 펌프 상태로 직접 옮긴다."""
        a = self._extra_by.get(equip_id)
        if a is not None:
            return a.physical_parameters(mode, equip_id)
        if equip_id in self.fault_targets.get(mode.id, ()):     # 계산 전용 확장: 트윈 시나리오 허용 목록 대신 회로에 직접 묻는다
            params = dict(self.adapters[self.circuit_of[equip_id]].physical_parameters(mode, equip_id))
        else:
            params = dict(self.physics.physical_parameters(mode, equip_id))
        if self.physics.roles.get(equip_id) == "source":
            params.update({n: tp for n, tp in hydraulic_plan(mode).items() if n == "discharge_valve_restriction"})
        return params

    def fault_support(self, mode: FaultMode, equip_id: str) -> tuple[bool, str | None, str]:
        """(지원 여부, 미지원 사유, 근거 physics|empirical)."""
        physical = bool(self._physical(mode, equip_id))
        note = self.physics.scenario_exclusions.get(f"{equip_id}/{mode.id}")
        if note:
            return False, note, "empirical"
        if any(e.shape in TRANSIENT for s in mode.stages for e in s.effects + s.propagate.driven + s.propagate.utility):
            return False, "충격(spike)·맥동(wobble) 효과가 있어 정적 대표값을 정의하지 않습니다(서지·액체 유입 등). 시간 응답(D6) 이후 검토합니다.", "empirical"
        if physical:
            return True, None, "physics"
        if mode.physics_only:
            return False, "물리 회로가 있어야 효과가 생기는 모드인데 이 설비에서는 회로 파라미터를 움직이지 않습니다.", "empirical"
        if equip_id in self.physics.roles and not mode.basis:
            return False, "물리 회로 설비의 경험 신호 전용 모드라 회로 해와 겹치지 않게 트윈과 같이 주입하지 않습니다.", "empirical"
        return True, None, "empirical"

    def faults_of(self, equip_id: str) -> list[dict]:
        e = self.by_id[equip_id]
        out = []
        for mode in self.modes.values():
            if not self._applies(mode, equip_id):
                continue
            ok, reason, basis = self.fault_support(mode, equip_id)
            out.append({"mode": mode.id, "name": mode.name, "supported": ok, "reason": reason, "basis": basis,
                        "interaction": mode.boost.rule if mode.boost and mode.boost.rule else None,
                        "stages": [{"id": s.id, "name": s.name} for s in mode.stages],
                        "default_stage": mode.stages[-1].id,
                        "parameters": sorted(self._physical(mode, equip_id))})
        return out

    # --- 카탈로그 ----------------------------------------------------------------------------------
    def catalog(self) -> dict:
        equipment = []
        for e in self.plant.equipment:
            role = self.roles.get(e.equip_id)
            cid = self.circuit_of.get(e.equip_id)
            a = self.adapters.get(cid) if cid else None
            equipment.append({
                "equip_id": e.equip_id, "name": e.name, "type": e.type, "unit_id": e.unit_id, "hydraulic_role": role,
                "circuit_id": cid, "driven_by": e.driven_by,
                "stoppable": isinstance(a, PhysicsAdapter) and role == "source",
                "note": self.extra_notes.get(e.equip_id) or self.physics.exclusions.get(e.equip_id),
                "faults": self.faults_of(e.equip_id),
                "tags": [{"tag_id": t.tag_id, "signal": t.signal, "unit": t.unit, "decimals": t.decimals,
                          "normal": self.normal_values[(e.equip_id, t.tag_id)],
                          "limits": t.limits.model_dump(exclude_none=True) if t.limits else {}} for t in e.tags]})
        return {
            "plant_id": self.plant.plant.plant_id, "plant": self.key, "synthetic": True, "model_version": self.model_version,
            "circuits": self.physics.circuits() + [c for a in self.extra for c in a.circuits()],
            "parameters": [p.public() for p in self.params.values()],
            "stop_options": {
                "series": {"handling": ["block", "pass", "bypass"], "default": None,
                           "resistance_ratio": {"min": STOP_RESISTANCE_RANGE[0], "max": STOP_RESISTANCE_RANGE[1],
                                                "unit": "단 정격 양정 대비", "required_for": ["pass", "bypass"]},
                           "note": "직렬 정지 처리와 저항은 근거 자료가 없는 가정이며 사용자가 반드시 고릅니다. 결과에 가정으로 표시합니다."},
                "parallel": {"handling": [], "note": "병렬 정지는 체크밸브가 닫힌 상태로 계산합니다(처리 선택 없음)."}},
            "interactions": self.interactions(),
            "rules": RULES,
            "equipment": equipment,
        }

    def interactions(self) -> list[dict]:
        """교호작용 I1~I6 의 이 공장 대상 설비와 정적 계산 지원 여부."""
        from simulator.interactions import RULES as IDS, targets
        out = []
        for rule in IDS:
            eqs = targets(rule, self.plant, self.modes)
            if rule in ("I1", "I2"):
                how, ok, reason = "계산 태그 식(Derive.value)으로 입력 변화에 따라 계산합니다.", True, None
            else:
                base = next(m for m in self.modes.values() if m.boost and m.boost.rule == rule)
                ok = all(self.fault_support(base, e)[0] for e in eqs) if eqs else False
                reasons = {self.fault_support(base, e)[1] for e in eqs} - {None}
                how, reason = f"{base.id} 고장의 정적 효과에 배율을 곱합니다.", "; ".join(sorted(reasons)) or None
            out.append({"rule": rule, "equipment": eqs, "supported": ok and bool(eqs), "how": how, "reason": reason})
        return out

    # --- 계산 ------------------------------------------------------------------------------------
    def calculate(self, request: dict) -> dict:
        try:
            return self._calculate(request)
        except CalcError:
            raise
        except Exception as exc:        # 엔진 중단: mock 으로 대체하지 않고 오류로 돌려준다
            raise CalcError("engine_error", f"계산 중 엔진이 중단되었습니다: {type(exc).__name__}: {exc}") from exc

    def _calculate(self, request: dict, baseline: bool = False) -> dict:
        if not isinstance(request, dict):
            raise CalcError("input_error", "요청 본문은 객체여야 합니다.")
        unknown = set(request) - {"causes", "stops", "faults", "sensor_overrides"}
        if unknown:
            raise CalcError("input_error", f"알 수 없는 필드: {', '.join(sorted(unknown))}")
        causes = self._read_list(request, "causes", ("parameter", "value"))
        stops = self._read_list(request, "stops", ("equip_id",), ("handling", "resistance_ratio"))
        faults = self._read_list(request, "faults", ("equip_id", "mode"), ("stage",))
        overrides = self._read_list(request, "sensor_overrides", ("equip_id", "tag_id", "value"))

        circuits: dict[str, _Inputs] = {cid: _Inputs() for cid in self.adapters}
        assumptions: list[dict] = []
        control = self.control
        speed_changed: dict[str, float] = {}
        cause_z: dict[TagKey, float] = {}
        touched: dict[tuple[str, str], str] = {}        # (설비, 물리 파라미터) → 바꾼 입력 (충돌 판정)

        # 1) 원인 값
        seen = set()
        for c in causes:
            pid, value = c["parameter"], self._number(c["value"], f"causes[{c['parameter']}].value")
            p = self.params.get(pid)
            if p is None:
                raise CalcError("input_error", f"지원하지 않는 원인 파라미터: {pid}")
            if pid in seen:
                raise CalcError("conflict", f"같은 원인 파라미터를 두 번 바꿨습니다: {pid}", parameter=pid)
            seen.add(pid)
            if not p.lo - EPS <= value <= p.hi + EPS:
                raise CalcError("out_of_range", f"{pid} = {value:g} 은 범위 {p.lo:g}~{p.hi:g} 밖입니다.", parameter=pid,
                                min=p.lo, max=p.hi)
            if p.circuit is None:
                speed_changed[p.owner] = value
                continue
            # 단일 회로 팬: 회전수 태그도 원인 값대로. 결합 회로 펌프 회전수 태그는 엔진이 계산한다(2026-11-27, 두 번 곱하지 않게)
            if p.name == "speed_ratio" and p.owner in self.speed_tag and not isinstance(self.adapters[p.circuit], PhysicsAdapter):
                k = self.speed_tag[p.owner]
                cause_z[k] = (value - 1) * self.control[k][0] / self.control[k][1]
            x = self._inputs_for(p, value)
            circuits[p.circuit].amounts.update(x.amounts)
            circuits[p.circuit].values.update(x.values)
            target = (self.adapters[p.circuit].primary, "valve_restriction") if p.name == "control_valve_opening" else (p.owner, p.name)
            touched[target] = f"원인 값 {pid}"
        if speed_changed:
            control = self._operating(speed_changed)

        # 2) 정지
        stopped = set()
        for s in stops:
            eid = s["equip_id"]
            cid = self.circuit_of.get(eid)
            a = self.adapters.get(cid) if cid else None
            if not isinstance(a, PhysicsAdapter) or self.physics.roles.get(eid) != "source":
                raise CalcError("unsupported", f"{eid}: 정지는 결합 회로의 입력 펌프에서만 계산합니다.", equip_id=eid)
            if eid in stopped:
                raise CalcError("conflict", f"{eid} 정지를 두 번 지정했습니다.", equip_id=eid)
            stopped.add(eid)
            for name in ("speed_ratio", "discharge_valve_opening", "strainer_blockage", "impeller_wear"):
                if f"{eid}/{name}" in seen:
                    raise CalcError("conflict", f"{eid}: 정지한 펌프에 {name} 을 함께 바꿀 수 없습니다.", equip_id=eid)
            series = a.case.station.topology == "series"
            handling, ratio = s.get("handling"), s.get("resistance_ratio")
            if not series:
                if handling is not None or ratio is not None:
                    raise CalcError("input_error", f"{eid}: 병렬 정지는 처리 방식과 저항을 받지 않습니다.", equip_id=eid)
                circuits[cid].stops[eid] = None
                assumptions.append({"kind": "parallel_stop", "equip_id": eid,
                                    "text": "병렬 펌프 정지: 체크밸브가 닫혀 역류가 없다고 가정합니다(동특성 미계산)."})
                continue
            if handling not in ("block", "pass", "bypass"):
                raise CalcError("input_error", f"{eid}: 직렬 정지는 handling(block·pass·bypass)을 반드시 골라야 합니다. 기본값이 없습니다.",
                                equip_id=eid)
            if handling == "block":
                if ratio is not None:
                    raise CalcError("input_error", f"{eid}: block 은 저항을 받지 않습니다.", equip_id=eid)
                circuits[cid].stops[eid] = Stopped(mode="block")
            else:
                if ratio is None:
                    raise CalcError("input_error", f"{eid}: {handling} 는 resistance_ratio(단 정격 양정 대비)가 필요합니다.", equip_id=eid)
                ratio = self._number(ratio, f"stops[{eid}].resistance_ratio")
                if not STOP_RESISTANCE_RANGE[0] <= ratio <= STOP_RESISTANCE_RANGE[1]:
                    raise CalcError("out_of_range", f"{eid}: resistance_ratio {ratio:g} 은 범위 "
                                    f"{STOP_RESISTANCE_RANGE[0]:g}~{STOP_RESISTANCE_RANGE[1]:g} 밖입니다.", equip_id=eid)
                unit = next(u for u in a.case.station.units if u.unit_id == eid)
                rho = a.case.network.density_kg_m3
                r_pa = ratio * rho * GRAVITY_M_S2 * unit.pump.rated_head_m / unit.pump.rated_flow_m3_s ** 2
                circuits[cid].stops[eid] = Stopped(mode=handling, resistance_pa_s2_m6=r_pa)
            assumptions.append({"kind": "series_stop", "equip_id": eid, "handling": handling, "resistance_ratio": ratio,
                                "text": f"직렬 펌프 정지 처리 '{handling}'"
                                        + (f", 저항 = 단 정격 양정의{ratio * 100:g} %" if ratio is not None else "")
                                        + "는 사용자가 고른 가정이며 근거 자료가 없습니다."})

        # 3) 고장
        plans: list[tuple[str, FaultMode, set[str], dict[TagKey, TagPlan], dict[TagKey, TagPlan]]] = []
        faulted: dict[str, str] = {}
        busy: dict[str, str] = {}
        for f in faults:
            eid, mid = f["equip_id"], f["mode"]
            if eid not in self.by_id:
                raise CalcError("input_error", f"알 수 없는 설비: {eid}")
            mode = self.modes.get(mid)
            if mode is None or not self._applies(mode, eid):
                raise CalcError("input_error", f"{eid} 에 적용할 수 없는 고장 모드: {mid}")
            ok, reason, _ = self.fault_support(mode, eid)
            if not ok:
                raise CalcError("unsupported", f"{eid}/{mid}: {reason}", equip_id=eid, mode=mid)
            if eid in faulted:
                raise CalcError("conflict", f"{eid}: 한 설비에는 고장을 하나만 걸 수 있습니다({faulted[eid]}, {mid}).", equip_id=eid)
            faulted[eid] = mid
            if eid in stopped:
                raise CalcError("conflict", f"{eid}: 정지한 펌프에는 고장을 걸 수 없습니다.", equip_id=eid)
            ids = [s.id for s in mode.stages]
            stage = f.get("stage") or ids[-1]
            if stage not in ids:
                raise CalcError("input_error", f"{mid} 에 없는 단계: {stage} (가능: {', '.join(ids)})")
            included = set(ids[: ids.index(stage) + 1])
            params = self._physical(mode, eid)
            if params:
                cid = self.circuit_of[eid]
                if cid in busy:
                    raise CalcError("conflict", f"회로 {cid} 에는 물리 고장을 하나만 걸 수 있습니다({busy[cid]}, {eid}/{mid}).",
                                    circuit_id=cid)
                busy[cid] = f"{eid}/{mid}"
                for name, tp in params.items():
                    if (eid, name) in touched:
                        raise CalcError("conflict", f"{eid}/{name} 을 {touched[(eid, name)]} 과 고장 {mid} 가 함께 바꿉니다.",
                                        equip_id=eid, parameter=name)
                    amount = sum(a.z for a in tp.applied if a.stage in included)
                    if not amount:
                        continue
                    if name == "discharge_valve_restriction":
                        if (eid, "discharge_valve_opening") in touched:
                            raise CalcError("conflict", f"{eid}: 토출 차단밸브 개도를 {touched[(eid, 'discharge_valve_opening')]} 과 "
                                            f"고장 {mid} 가 함께 바꿉니다.", equip_id=eid, parameter="discharge_valve_opening")
                        opening = round(1 - min(1.0, amount) * (1 - BLOCK_VALVE_MIN), 6)
                        circuits[cid].values[f"{eid}/discharge_valve_opening"] = opening
                        outside = opening < VALVE_RANGE[0] - EPS
                        assumptions.append({"kind": "extrapolated" if outside else "fault_valve", "equip_id": eid,
                                            "text": f"{eid} 토출 차단밸브 부분 닫힘: 고장 단계를 개도 {opening:g}(정상 1)로 옮긴 가정입니다."
                                                    + (f" 검증 하한 {VALVE_RANGE[0]:g} 아래라 합성 곡선을 넘겨 쓴 외삽이며 검증하지 않았습니다."
                                                       if outside else "")})
                    else:
                        circuits[cid].amounts[(eid, name)] = min(1.0, amount)
            if eid in speed_changed:
                speed = next((t.tag_id for t in self.by_id[eid].tags if t.signal == "speed"), None)
                if any(s.signal == "speed" for st in mode.stages if st.id in included for s in st.effects):
                    raise CalcError("conflict", f"{eid}: 회전수 원인 값과 회전수를 바꾸는 고장 {mid} 를 함께 쓸 수 없습니다.",
                                    equip_id=eid, tag_id=speed)
            twin = self._twin_plan(mode, eid, control)
            full = plan(self.plant, mode, eid, control)
            plans.append((eid, mode, included, twin, full))

        # 4) 회로 풀이
        balances, centers, ratio, split, solved, collapsed = [], {}, {}, set(), set(), []
        order = sorted(circuits, key=lambda cid: not isinstance(self.adapters[cid], PhysicsAdapter))
        for cid in order:
            x = circuits[cid]
            src = self.split_from.get(cid)
            f = ratio.get(src, 1.0)
            if x.empty() and abs(f - 1) < EPS:
                centers.update(self._normal_centers[cid])
                if isinstance(self.adapters[cid], PhysicsAdapter):
                    ratio[cid] = 1.0
                continue
            if abs(f - 1) >= EPS:
                c, bal = self._single(self.adapters[cid], x, f * self._normal_flow[cid])
                split.update(c)
                a = self.adapters[cid]
                unreached = bal.pop("_unreached", False)
                assumptions.append({
                    "kind": "fixed_split", "equip_id": a.primary, "circuit_id": cid, "source_circuit_id": src, "flow_ratio": f,
                    "text": f"{a.primary}: 고정 분배 대용모델. 상류 {src} 유량비 {f:.3f} 를 따라 유량을 정상의 {f * 100:.1f} % 로 맞추는 "
                            "토출 제어밸브를 가정하고 압력·전류는 펌프 곡선으로 계산했습니다(탑·드럼 액위·환류 계산 없음)."
                            + (" 밸브를 다 열어도 목표 유량에 못 미쳐 펌프가 낼 수 있는 최대 유량으로 계산했습니다." if unreached else "")})
            else:
                c, bal = self._centers(cid, x)
            collapsed += bal.pop("_collapse", [])
            drum = bal.pop("_drum", None)
            if drum is not None:
                rising = drum["state"] == "level_rising"
                assumptions.append({
                    "kind": "drum_level", "equip_id": drum["equip_id"], "circuit_id": cid, "state": drum["state"],
                    "level_valve_opening": round(drum["opening"], 4),
                    "text": f"{drum['equip_id']} 흡입 드럼(탑·드럼 대용모델): 액위 조절 밸브가 "
                            + ("다 열려도 유출이 유입보다 적어 액위가 계속 오릅니다." if rising else "최소 개도에서도 유출이 유입보다 많아 액위가 계속 내려갑니다.")
                            + " 정상 상태가 없어 이 값은 밸브 끝 개도에서 계산한 순간값입니다."})
            ctl = bal.pop("_control", None)
            if ctl is not None:
                a = self.adapters[cid]
                assumptions.append({
                    "kind": "flow_control", "equip_id": a.primary, "circuit_id": cid, "valve_opening": round(ctl["opening"], 4),
                    "saturated": ctl["saturated"],
                    "text": f"{cid}: 유량 제어 가정(가상 제어밸브 정상 개도 70 %, 동적 손실의 1/3). 제어밸브 개도 " + self._opening_text(ctl)
                            + (". 밸브가 끝(10 %·100 %)에 닿아 정상 유량을 지키지 못했습니다(포화)." if ctl["saturated"] else " 로 정상 유량을 지켰습니다.")})
            if isinstance(self.adapters[cid], PhysicsAdapter):        # 제어가 유량을 지켰으면(포화 아님) 반복 잔차를 하류로 넘기지 않는다
                ratio[cid] = 1.0 if ctl is not None and not ctl["saturated"] else bal["total_flow_m3_h"] / self._normal_flow[cid]
            centers.update(c)
            solved.update(c)
            balances.append(bal)

        severe = [c["parameter"] for c in causes if self.params[c["parameter"]].extrapolated(c["value"])]
        if severe:
            assumptions.append({"kind": "extrapolated", "parameters": severe,
                                "text": "검증 범위 밖 심각 구간 입력입니다(" + ", ".join(severe) + "). 합성 곡선을 넘겨 쓴 외삽이며 검증하지 않았습니다."})
        if collapsed:
            assumptions.append({"kind": "cavitation_collapse", "equipment": collapsed,
                                "text": ", ".join(collapsed) + ": 캐비테이션 붕괴 외삽 구간입니다. 흡입 여유가 모자라 양정이 무너지고 유량이 NPSH 로 "
                                        "제한된다고 보고 계산했습니다(검증 상한을 넘은 합성 외삽)."})

        # 5) 태그 값
        values, basis, status, notes = self._evaluate(plans, centers, control, cause_z, split, solved)
        normal = None if baseline else self.normal_values
        # 6) 센서 오류 (표시값만)
        seen_tags = set()
        for o in overrides:
            key = (o["equip_id"], o["tag_id"])
            if key not in self.tags:
                raise CalcError("input_error", f"알 수 없는 태그: {key[0]}/{key[1]}")
            if key in seen_tags:
                raise CalcError("conflict", f"같은 태그의 센서값을 두 번 바꿨습니다: {key[0]}/{key[1]}")
            seen_tags.add(key)
            values[key] = self._number(o["value"], f"sensor_overrides[{key[1]}].value")
            basis[key], status[key] = "sensor_override", "changed"
            notes[key] = "센서 오류로 표시값만 바꿨습니다. 다른 값은 이 값으로 다시 계산하지 않습니다(역산은 D12)."
        return self._response(values, basis, status, notes, normal, balances, assumptions, plans, request)

    # --- 보조 -------------------------------------------------------------------------------------
    @staticmethod
    def _read_list(request: dict, name: str, required: tuple, optional: tuple = ()) -> list[dict]:
        items = request.get(name) or []
        if not isinstance(items, list):
            raise CalcError("input_error", f"{name} 는 배열이어야 합니다.")
        if len(items) > MAX_ITEMS:
            raise CalcError("input_error", f"{name} 는 {MAX_ITEMS}개까지 받습니다.")
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                raise CalcError("input_error", f"{name}[{i}] 는 객체여야 합니다.")
            missing = [k for k in required if k not in item]
            extra = set(item) - set(required) - set(optional)
            if missing or extra:
                raise CalcError("input_error", f"{name}[{i}]: 필수 {', '.join(required)}"
                                + (f", 선택 {', '.join(optional)}" if optional else "")
                                + (f" (빠짐: {', '.join(missing)})" if missing else "")
                                + (f" (알 수 없음: {', '.join(sorted(extra))})" if extra else ""))
            for k in ("equip_id", "mode", "stage", "tag_id", "parameter", "handling"):
                if k in item and item[k] is not None and not isinstance(item[k], str):
                    raise CalcError("input_error", f"{name}[{i}].{k} 는 문자열이어야 합니다.")
        return items

    @staticmethod
    def _number(v: Any, where: str) -> float:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise CalcError("input_error", f"{where} 는 유한한 숫자여야 합니다.")
        return float(v)

    def _operating(self, speeds: dict[str, float]) -> dict[TagKey, tuple[float, float]]:
        """회로 밖 구동기 회전수 → 운전점(Operating)을 넣은 사본의 control_by_key (상사 법칙). 원본 마스터는 바꾸지 않는다."""
        equipment = []
        for e in self.plant.equipment:
            if e.equip_id in speeds:
                design = next(t.control.mean for t in e.tags if t.signal == "speed")
                e = e.model_copy(update={"operating": Operating(speed_rpm=design * speeds[e.equip_id])})
            equipment.append(e)
        return self.plant.model_copy(update={"equipment": equipment}).control_by_key()

    def _twin_plan(self, mode: FaultMode, eid: str, control) -> dict[TagKey, TagPlan]:
        """트윈 모드 계획(PhysicsBundle.tag_plan, general=False). 회로 밖 설비는 운전점이 바뀐 control 로 다시 만든다
        (같은 필터: 공정 전파 제외, 다른 회로 물리 태그 제외). 회로 설비는 운전점이 없어 묶음 계획을 그대로 쓴다."""
        a = self._extra_by.get(eid)
        if a is not None:      # 계산 전용 단일 회로: 물리로 푼 태그는 경험 효과를 겹치지 않는다(SingleAdapter.tag_plan 과 같은 규칙)
            physical = a.physical_parameters(mode, eid)
            return {k: tp for k, tp in plan(self.plant, mode, eid, control).items()
                    if tp.via != "process" and k not in self.physics.tag_keys and not (physical and k in a.tag_keys)}
        if eid in self.physics.roles or control is self.control:
            return self.physics.tag_plan(mode, eid)
        return {k: tp for k, tp in plan(self.plant, mode, eid, control).items()
                if tp.via != "process" and k not in self.physics.tag_keys}

    @staticmethod
    def _static_z(tp: TagPlan, included: set[str], shapes=HELD) -> float:
        return sum(a.z for a in tp.applied if a.stage in included and a.effect.shape in shapes)

    def _evaluate(self, plans, centers, control, cause_z, split=frozenset(), solved=frozenset()):
        """태그별 정적 값. 생성기(PlantGenerator.generate)의 잡음 없는 기댓값과 같은 규칙: 물리 태그 = μ + (해 − 정상 해),
        그 밖 = μ(운전점 반영) + σ·효과, 피구동 speed = 구동기 z 복사, 계산 태그 = Derive.value(입력 값) + σ·효과.
        cause_z: 원인 값이 직접 옮긴 태그의 z (단일 회로 팬의 회전수). 교호작용 by 태그 z 에도 더한다."""
        values: dict[TagKey, float | None] = {}
        basis: dict[TagKey, str] = {}
        status: dict[TagKey, str] = {}
        notes: dict[TagKey, str] = {}
        static: dict[TagKey, float] = dict(cause_z)       # by·요인 태그의 사건 효과 z (step·ramp)
        for _, _, included, twin, _ in plans:
            for k, tp in twin.items():
                if tp.via != "physics":
                    static[k] = static.get(k, 0.0) + self._static_z(tp, included)
        fx: dict[TagKey, float] = dict(cause_z)
        for _, mode, included, twin, full in plans:
            for k, tp in twin.items():
                if tp.via == "physics":
                    continue
                z = self._static_z(tp, included)
                b = tp.boost
                if b is not None and b.factors:
                    gain = factor_gain(b, lambda key: static.get(key, 0.0))
                    z += (gain - 1) * self._static_z(tp, included, tuple(x for x in (b.shapes or HELD) if x in HELD))
                elif b is not None and z:
                    released = b.hold is not None and any(
                        a.stage in included and a.effect.to_limit and a.effect.to_limit.startswith("alarm") for a in tp.applied)
                    z = boosted(b, z, static.get(b.by, 0.0), math.inf if released else -math.inf)
                fx[k] = fx.get(k, 0.0) + z
            # 공정 흐름 하류 경험 전파는 계산하지 않는다(트윈과 같음): 그 태그는 계산하지 못한 값으로 구분한다
            for k, tp in full.items():
                if (tp.via == "process" and k not in twin and k not in self.physics.tag_keys
                        and any(a.stage in included for a in tp.applied)):
                    notes.setdefault(k, f"{mode.id} 의 공정 흐름 하류 경험 전파는 정적 계산에서 다루지 않습니다(트윈과 같음).")
        # 이 요청에서 회로를 실제로 푼 태그(고정 분배·자체 원인)는 값을 낸다. 공정 전파 경험 효과는 여전히 계산하지 않으므로
        # 고정 분배 태그만 안내를 바꾸고, 그 밖은 '공정 전파 미계산' 안내를 남긴다. 회로를 풀지 않은 태그는 지금처럼 미계산이다.
        skipped = {k for k in notes if abs(fx.get(k, 0.0)) < EPS and k not in solved}

        def mark(k: TagKey, note: str | None = None) -> None:
            values[k], basis[k], status[k] = None, "not_modeled", "not_computed"
            if note:
                notes[k] = note

        driven = {self.speed_tag[e] for e in self.driver_speed}
        for k in self.order:
            t = self.tags[k]
            if k in driven or (t.derive is not None and k not in centers):
                continue
            if k in skipped:
                mark(k)
                continue
            mu, sg = control[k]
            if k in centers:
                c = centers[k]
                if c is None:
                    mark(k, "체절(dead-headed) 운전은 엔진이 축동력을 계산하지 않아 전류를 내지 않습니다.")
                    continue
                values[k] = self.control[k][0] + (c - self._normal_centers[self.circuit_of[k[0]]][k]) + sg * fx.get(k, 0.0)
                basis[k] = "empirical" if k in split else "physics"
                if k in split:
                    notes[k] = "고정 분배 대용모델: 상류 유량비를 따르는 유량에서 펌프 곡선으로 계산한 값입니다."
            else:
                values[k] = mu + sg * fx.get(k, 0.0)
                basis[k] = "physics" if k in cause_z else "empirical"
        pending = dict(self.driver_speed)
        while pending:             # 피구동 speed: 구동기 값이 정해진 것부터 (축 사슬 순서)
            ready = {e: dk for e, dk in pending.items() if dk in values}
            if not ready:
                for e in pending:
                    mark(self.speed_tag[e], "구동기 회전수를 계산하지 못했습니다.")
                break
            for e, dk in ready.items():
                k = self.speed_tag[e]
                del pending[e]
                if values[dk] is None:
                    mark(k, "구동기 회전수를 계산하지 못했습니다.")
                    continue
                dmu, dsg = control[dk]
                mu, sg = control[k]
                values[k], basis[k] = mu + sg * (values[dk] - dmu) / dsg, basis[dk]
        for k in self.order:
            t = self.tags[k]
            if t.derive is None or k in centers:
                continue
            if k in skipped:
                mark(k)
                continue
            ins = [(k[0], i) for i in t.derive.inputs]
            xs = [values.get(i) for i in ins]
            if any(v is None for v in xs):
                mark(k, "계산 태그의 입력을 계산하지 못했습니다.")
                continue
            values[k] = t.derive.value(xs, [self.tags[i].control.mean for i in ins], t.control.mean) + control[k][1] * fx.get(k, 0.0)
            basis[k] = "physics" if any(basis.get(i) == "physics" for i in ins) and not fx.get(k) else "empirical"
        return values, basis, status, notes

    def _response(self, values, basis, status, notes, normal, balances, assumptions, plans, request) -> dict:
        if normal is None:      # 정상 운전점 자체를 만드는 호출
            return {"_values": values}
        tags = []
        stages: dict[str, str] = {e.equip_id: "normal" for e in self.plant.equipment}
        changed_eq: dict[str, list[str]] = {}
        for k in self.order:
            t = self.tags[k]
            v, n = values.get(k), normal[k]
            st = status.get(k) or ("changed" if v is not None and abs(v - n) > EPS * max(1.0, abs(n)) else "unchanged")
            stage = None if v is None else stage_of(v, t.limits)
            if stage is not None and RANK[stage] > RANK[stages[k[0]]]:
                stages[k[0]] = stage
            if st == "changed":
                changed_eq.setdefault(k[0], []).append(k[1])
            tags.append({"equip_id": k[0], "tag_id": k[1], "signal": t.signal, "unit": t.unit, "decimals": t.decimals,
                         "normal": n, "value": None if v is None else v + 0.0, "delta": None if v is None else v - n + 0.0,
                         "status": st, "basis": basis.get(k, "empirical"), "stage": stage, "note": notes.get(k)})
        direct = ({self.params[c["parameter"]].owner for c in request.get("causes") or []}
                  | {s["equip_id"] for s in request.get("stops") or []} | {f["equip_id"] for f in request.get("faults") or []}
                  | {o["equip_id"] for o in request.get("sensor_overrides") or []})
        for cid in [d for d in direct if d in self.adapters]:      # 회로 단위 원인(경계압·제품 밸브)은 그 회로 설비 전체가 직접 대상
            direct |= set(self.adapters[cid].roles)
        circuits = {self.circuit_of.get(d) for d in direct} - {None}
        connected = []
        for eid, changed in changed_eq.items():
            if eid in direct:
                continue
            via = ("circuit" if self.circuit_of.get(eid) in circuits
                   else "shaft" if any(self._shaft_linked(eid, d) for d in direct) else "empirical")
            connected.append({"equip_id": eid, "via": via, "circuit_id": self.circuit_of.get(eid), "changed_tags": changed})
        if any(m.basis for _, m, *_ in plans):
            assumptions.append({"kind": "empirical_basis", "text": "가정 기반(basis) 경험 효과가 포함되었습니다. 물리 계산이 아닌 합성 효과입니다."})
        if plans:
            assumptions.append({"kind": "static_representative",
                                "text": "고장 효과는 고른 단계까지의 step·ramp 효과가 끝까지 진행한 정적 대표값입니다(시간 응답 없음)."})
        if any(self.params[c["parameter"]].basis == "empirical" for c in request.get("causes") or []):
            assumptions.append({"kind": "operating_affinity",
                                "text": "회로 밖 구동기 회전수는 운전점 상사 법칙으로 기준값만 옮긴 경험 계산입니다."})
        return {
            "plant_id": self.plant.plant.plant_id, "plant": self.key, "synthetic": True, "model_version": self.model_version,
            "equipment": [{"equip_id": e, "stage": st} for e, st in stages.items()],
            "connected_equipment": connected, "circuits": balances, "assumptions": assumptions, "tags": tags,
        }

    def _chain(self, eid: str) -> set[str]:
        """축 사슬 위쪽 구동기들."""
        out, e = set(), self.by_id[eid]
        while e.driven_by and e.driven_by not in out:
            out.add(e.driven_by)
            e = self.by_id[e.driven_by]
        return out

    def _shaft_linked(self, a: str, b: str) -> bool:
        if a not in self.by_id or b not in self.by_id:
            return False
        ca, cb = self._chain(a) | {a}, self._chain(b) | {b}
        return bool(ca & cb)


RULES = {
    "stateless": "요청마다 정상 운전점에서 다시 계산합니다. 이전 요청을 기억하지 않습니다.",
    "max_physical_faults_per_circuit": 1,
    "max_faults_per_equipment": 1,
    "conflicts": [
        "같은 (설비, 물리 파라미터)를 원인 값과 고장이 함께 바꾸면 거절합니다(제품 밸브 개도 ↔ valve_restriction 포함).",
        "정지한 펌프에는 고장·회전수·밸브·막힘·마모를 함께 줄 수 없습니다.",
        "구동기 회전수 원인 값과 회전수를 바꾸는 고장(OP-LOAD-CHANGE 등)은 함께 쓸 수 없습니다.",
        "같은 원인 파라미터·같은 태그의 센서값을 두 번 바꿀 수 없습니다.",
    ],
    "overlap": "회로가 다른 변경은 각 회로에서 독립으로 풀고, 같은 회로의 원인 값들은 한 번에 함께 풉니다. "
               "경험 효과는 태그별로 더하고 교호작용 배율은 같은 요청의 다른 효과(by·요인 태그)를 봅니다.",
    "sensor_override": "센서 오류: 그 태그의 표시값만 바꿉니다. 계산 태그 입력으로 다시 쓰지 않습니다.",
}


class Registry:
    """다섯 공장 계산기. 시작할 때 모두 읽는다(읽기 전용, 요청 사이에 공유해도 상태가 섞이지 않는다)."""

    def __init__(self, plants=PLANTS, config: Path = ROOT / "config") -> None:
        self.calcs = {k: PlantCalc(k, config) for k in plants}

    def get(self, key: str) -> PlantCalc | None:
        return self.calcs.get(key.lower())
