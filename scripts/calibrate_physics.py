"""HOU·RFCC·RHDS·VRDS 대표 수력 회로 설정을 각 플랜트 마스터의 운전점에서 역산한다.

입력은 두 가지다.
1. 마스터(`config/plant_<key>.yaml`)의 설비·태그·공정 간선과 운전점 μ(`control_by_key`), 경고·경보·정지 하한.
2. 아래 `DESIGN` 의 합성 설계 선택(밀도·회전수·효율·체절 비·배관 치수·분기 압력 비율). 모두 synthetic assumption 이다.

입력 펌프는 두 대다(RFCC 병렬, HOU·RHDS·VRDS 직렬. PU-D5a). 토출 라인 제원은 LINE_* 상수다.
출력 YAML 의 정상 운전 해는 마스터 μ(첫 펌프 흡입압·펌프당 유량, 최종 토출압, 후속 펌프 흡입·토출압·유량)를 재현한다.
직렬 1단 토출압은 입력이 아니라 같은 곡선 두 단으로 나눈 결과다(마스터 1단 토출 μ 는 그 값을 옮긴 것).
고장 크기(스트레이너 막힘 저항·임펠러 양정 감소·밸브 저항 비중)는 해당 태그의 하한 기준을 넘는 지점으로 정한다.
실행: `uv run --locked python -m scripts.calibrate_physics hou` (표준 출력으로 YAML). `--write` 는 config/ 에 쓴다.
CDU 설정(`physics_cdu_integrated.yaml`)은 이 스크립트로 만들지 않는다(별도 D2-A 작성본. PU-D5a 의 두 펌프 값도 그 파일 주석의 식으로 손 계산).
"""
from __future__ import annotations

import argparse
import math
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import yaml

from common.config_models import PlantConfig, load_plant
from simulator.physics.pump import GRAVITY_M_S2
from simulator.physics_adapter import PRESSURE_PA, CoupledCase

ROOT = Path(__file__).resolve().parents[1]
ATM_PA = 101_325.0
DARCY = 0.025              # 상용 강관 난류 영역의 고정 Darcy 계수 (synthetic)
LINE_M, LINE_V, CHECK_K, GATE_K = 10.0, 2.0, 2.0, 0.2      # 펌프 토출 라인 (synthetic): 길이 m·설계 유속·스윙 체크밸브 K·게이트 전개 K
MARGIN_FLOOR = 0.85        # 고장 최대치에서 허용하는 최소 NPSH 여유비 (캐비테이션 모델 유효 범위 안, D2-B)


@dataclass(frozen=True)
class Design:
    """플랜트별 합성 설계 선택. 값의 근거는 공정 유체·펌프 형식에 대한 일반 범위이며 현장 자료가 아니다."""
    source: str
    second: str
    topology: str
    downstream: str
    density: float                 # kg/m³, 회로 전체 단일 밀도
    speed: float                   # 입력 펌프 rpm
    booster_speed: float           # 후속 펌프 rpm
    shutoff_ratio: float           # 입력 펌프 체절/정격 양정
    booster_shutoff_ratio: float
    efficiency: float
    booster_efficiency: float
    product_length: float          # 헤더 → 후속 펌프 등가 배관 m
    other_length: float            # 다른 등가 분기 m
    tail_length: float             # 후속 펌프 토출 배관 m
    velocity: float                # 배관 지름 선정용 설계 유속 m/s
    other_outlet_fraction: float   # 다른 분기 출구압 = 후속 흡입압 + f × (헤더압 − 후속 흡입압)
    tail_drop_fraction: float      # 후속 토출 배관 강하 = f × 후속 펌프 차압
    efficiency_loss: float         # 임펠러 마모 최대 효율 상대 감소 (입력·후속 공통)
    valve_opening_loss: float      # 밸브 제한 최대 개도 감소 비율
    # D2-B NPSH (synthetic): 유체 온도, 정격 NPSHr, 정상 여유비 NPSHa/NPSHr. 증기압은 정상 여유비에서 역산한다.
    feed_temp_k: float = 423.15
    feed_npshr_m: float = 6.0
    feed_margin: float = 3.0
    booster_temp_k: float | None = None   # None 이면 후속 펌프 흡입·취급 유체 온도 태그 μ
    booster_npshr_m: float = 3.5
    booster_margin: float = 2.0
    # 후속 펌프 흡입 드럼(탑·드럼 대용모델, 2026-11-26). 정상점은 직결 회로와 같고 고장 최대치는 드럼 회로로 역산한다.
    drum: bool = False


# 액위 조절 밸브 저항 비중: 다 열면 토출 총저항이 37.5 % 줄어든다(0.3 은 22.5 % 라 후속 고장 0.2 부터 포화, open-issues 104, 2026-11-26 사용자 결정).
DRUM_VALVE_SHARE = 0.5

DESIGN = {
    # HOU: VGO 원료 → 고압 반응부 → 분리·분별 → 미전환유 순환 펌프. 반응·분리 압력 강하를 등가 저항 하나로 본다.
    "hou": Design("P-2101", "P-2101-S2", "series", "P-2701", drum=True, density=900, speed=3580, booster_speed=3560, shutoff_ratio=1.18,
                  booster_shutoff_ratio=1.12, efficiency=0.68, booster_efficiency=0.70, product_length=260,
                  other_length=90, tail_length=60, velocity=2.2, other_outlet_fraction=0.12,
                  tail_drop_fraction=0.08, efficiency_loss=0.18, valve_opening_loss=0.55,
                   feed_temp_k=423.15, feed_npshr_m=6.5, feed_margin=3.0, booster_npshr_m=3.5, booster_margin=2.0),
    # RFCC: 상압잔사 원료 → 라이저 반응기 → 주 분류탑 → 탑정 환류 펌프. 분해·기화는 계산하지 않는다.
    "rfcc": Design("P-1101A", "P-1101B", "parallel", "P-1303A", drum=True, density=940, speed=3560, booster_speed=3560, shutoff_ratio=1.22,
                   booster_shutoff_ratio=1.18, efficiency=0.72, booster_efficiency=0.66, product_length=180,
                   other_length=70, tail_length=50, velocity=2.0, other_outlet_fraction=0.20,
                   tail_drop_fraction=0.15, efficiency_loss=0.20, valve_opening_loss=0.60,
                   feed_temp_k=473.15, feed_npshr_m=5.0, feed_margin=2.5, booster_temp_k=313.15, booster_npshr_m=3.0, booster_margin=2.2),
    # RHDS: 상압잔사 원료 → 고압 반응부 → 분리 → 분별탑 → 고온 잔사유 이송 펌프(4극 저속).
    "rhds": Design("P-3101", "P-3101-S2", "series", "P-3102", density=950, speed=3570, booster_speed=1780, shutoff_ratio=1.20,
                   booster_shutoff_ratio=1.20, efficiency=0.70, booster_efficiency=0.68, product_length=240,
                   other_length=85, tail_length=45, velocity=2.1, other_outlet_fraction=0.10,
                   tail_drop_fraction=0.18, efficiency_loss=0.20, valve_opening_loss=0.60,
                   feed_temp_k=473.15, feed_npshr_m=6.0, feed_margin=2.8, booster_npshr_m=3.5, booster_margin=1.8),
    # VRDS: 감압잔사 원료(고점도는 고정 Darcy로 단순화) → 고압 반응부 → 분리 → 분별탑 → 고온 잔사유 이송 펌프.
    "vrds": Design("P-4101", "P-4101-S2", "series", "P-4102", density=990, speed=3570, booster_speed=1780, shutoff_ratio=1.20,
                   booster_shutoff_ratio=1.18, efficiency=0.69, booster_efficiency=0.66, product_length=220,
                   other_length=80, tail_length=55, velocity=2.0, other_outlet_fraction=0.11,
                   tail_drop_fraction=0.14, efficiency_loss=0.22, valve_opening_loss=0.60,
                   feed_temp_k=493.15, feed_npshr_m=6.0, feed_margin=2.8, booster_npshr_m=3.5, booster_margin=1.8),
}


PATHS = {
    "hou": ["P-2101", "P-2101-S2", "MIX", "HX", "FURN", "REACT", "HX", "SEP", "STRIP", "FRAC", "P-2701"],
    "rhds": ["P-3101", "P-3101-S2", "MX", "HX", "HT", "RX", "HX", "SP", "FR", "P-3102"],
    "vrds": ["P-4101", "P-4101-S2", "MX", "HX", "HT", "RX", "HX", "SP", "FR", "P-4102"],
}


def process_path(plant: PlantConfig, start: str, end: str) -> list[str]:
    edges = [(a, b) for a, b, _, stream in plant.flow.edges if stream == "process"]
    queue, seen = deque([[start]]), {start}
    while queue:
        path = queue.popleft()
        if path[-1] == end:
            return path
        for a, b in edges:
            if a == path[-1] and b not in seen:
                seen.add(b)
                queue.append(path + [b])
    raise ValueError(f"{start} → {end} 공정 경로가 마스터에 없습니다.")


def area(diameter: float) -> float:
    return math.pi * diameter**2 / 4


def round_diameter(flow: float, velocity: float) -> float:
    """설계 유속에서 0.005 m 단위로 올린 내경."""
    return math.ceil(math.sqrt(4 * flow / (math.pi * velocity)) / 0.005) * 0.005


def pipe_k(resistance: float, density: float, length: float, diameter: float) -> float:
    """목표 저항 R(Pa·s²/m⁶)을 내는 국부 손실계수 합. 음수면 설계 선택이 맞지 않는다."""
    k = resistance * 2 * area(diameter)**2 / density - DARCY * length / diameter
    if k < 0:
        raise ValueError("배관 마찰만으로 목표 저항을 넘습니다. 길이·지름 설계를 바꾸세요.")
    return k


def lower_target(tag_limits: dict, mean: float) -> float | None:
    """고장 최대치의 목표값: 경보 하한과 정지 하한 사이(정지 하한이 없으면 경보 하한보다 경고~경보 간격의 절반 아래)."""
    alarm, trip, warn = tag_limits.get("alarm_lo"), tag_limits.get("trip_lo"), tag_limits.get("warn_lo")
    if alarm is None:
        return None if warn is None else warn - 0.5 * (mean - warn) * 0.2
    if trip is not None:
        return alarm - 0.4 * (alarm - trip)
    gap = (warn - alarm) if warn is not None else 0.1 * (mean - alarm)
    return alarm - 0.5 * gap


def bisect(fn, lo: float, hi: float, target: float, increasing: bool) -> float:
    """fn 이 단조인 구간에서 fn(x) = target 인 x. 해가 없는(역류·음의 절대압) x 는 목표를 지나친 쪽으로 본다."""
    for _ in range(80):
        mid = (lo + hi) / 2
        try:
            value = fn(mid)
        except ValueError:
            value = -math.inf if not increasing else math.inf
        if (value < target) == increasing:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def build(key: str) -> dict:
    d = DESIGN[key]
    plant = load_plant(ROOT / f"config/plant_{key}.yaml")
    control = plant.control_by_key()
    equipment = plant.equipment_by_id()
    tags = {eid: {t.signal: t for t in equipment[eid].tags} for eid in (d.source, d.second, d.downstream)}
    series = d.topology == "series"

    def mu(eid: str, signal: str) -> float:
        return control[(eid, tags[eid][signal].tag_id)][0]

    def abs_pa(eid: str, signal: str) -> float:
        return mu(eid, signal) * PRESSURE_PA[tags[eid][signal].unit] + ATM_PA

    rho = d.density
    rho_g = rho * GRAVITY_M_S2
    ps1, pd1 = abs_pa(d.source, "suction_pressure"), abs_pa(d.second, "discharge_pressure")     # 묶음의 흡입·최종 토출
    ps2, pd2 = abs_pa(d.downstream, "suction_pressure"), abs_pa(d.downstream, "discharge_pressure")
    unit_q, q2 = mu(d.source, "flow") / 3600, mu(d.downstream, "flow") / 3600
    q1 = unit_q if series else 2 * unit_q               # 헤더 전체 유량
    d_line = round_diameter(unit_q, LINE_V)
    line = {"line": {"length_m": LINE_M, "diameter_m": d_line, "darcy_factor": DARCY},
            "check_valve": {"diameter_m": d_line, "forward_k": CHECK_K}, "valve": {"diameter_m": d_line, "full_open_k": GATE_K}}
    line_dp = (DARCY * LINE_M / d_line + CHECK_K + GATE_K) * rho / (2 * area(d_line)**2) * unit_q**2
    header = pd1 - line_dp
    if not (q2 < q1 and ps1 < pd1 and ps2 < pd1 and ps2 < pd2):
        raise ValueError(f"{key}: 단일 헤더·두 분기 회로로 표현할 수 없는 운전점입니다.")
    q_other = q1 - q2

    head1, head2 = ((pd1 - ps1 + line_dp) / 2 if series else pd1 - ps1) / rho_g, (pd2 - ps2) / rho_g      # 직렬 단 양정은 같다
    d_product, d_other, d_tail = (round_diameter(q, d.velocity) for q in (q2, q_other, q2))
    r_product = (header - ps2) / q2**2                     # 헤더 → 후속 펌프 흡입 (배관 + 밸브)
    p_other = ps2 + d.other_outlet_fraction * (header - ps2)
    r_other = (header - p_other) / q_other**2
    p_tail_out = pd2 - d.tail_drop_fraction * (pd2 - ps2)
    r_tail = (pd2 - p_tail_out) / q2**2
    # 원료가 실제로 지나는 공정 순서(가열로·반응기 포함). 최단 경로는 열교환기 고온측 귀환 간선으로 반응부를 건너뛴다.
    path = PATHS.get(key) or process_path(plant, d.source, d.downstream)
    unit = equipment[d.source].unit_id
    plant_id = plant.plant.plant_id

    def npsh_block(temp: float, npshr: float, margin: float, inlet: float, flow: float, diameter: float) -> dict:
        v = flow / area(diameter)
        npsha = margin * npshr
        pv = inlet - rho_g * (npsha - v**2 / (2 * GRAVITY_M_S2))
        if pv <= 0:
            raise ValueError("정상 여유비를 만족하는 증기압이 음수입니다. 여유비·NPSHr 을 줄이세요.")
        return {"fluid_temp_k": temp, "vapor_pressure_pa": pv, "vapor_b_k": 4000.0, "suction_diameter_m": diameter,
                "npshr_rated_m": npshr, "npshr_zero_flow_fraction": 0.5, "onset_margin": 1.3, "steep_coefficient": 0.8,
                "max_head_loss": 0.6, "efficiency_coupling": 0.5}

    booster_temp = d.booster_temp_k or (mu(d.downstream, "process_temp") + 273.15 if "process_temp" in tags[d.downstream] else 323.15)
    # 절반 유량 펌프(RFCC)는 흡입 유속을 유지하는 지름(D4 사용자 결정 제원)
    feed_diameter = round_diameter(q1, 2.5) * (1 if series else math.sqrt(0.5))
    feed_npsh = npsh_block(d.feed_temp_k, d.feed_npshr_m, d.feed_margin, ps1, unit_q, feed_diameter)
    booster_npsh = npsh_block(booster_temp, d.booster_npshr_m, d.booster_margin, ps2, q2, round_diameter(q2, 2.5))

    def config(valve_share: float, strainer: float, head_loss: float, booster_strainer: float,
               booster_head_loss: float, second_strainer: float | None = None) -> dict:
        pump = {"reference_speed_rpm": d.speed, "rated_flow_m3_s": unit_q, "shutoff_head_m": head1 * d.shutoff_ratio,
                "rated_head_m": head1, "efficiency": d.efficiency}
        valve_k = valve_share * r_product * 2 * area(d_product)**2 / rho
        return {
            "plant_id": plant_id, "unit_id": unit, "equip_id": d.source,
            "suction_node": "virtual:suction-boundary", "discharge_node": "virtual:discharge-header",
            "provenance": "synthetic assumption",
            "downstream_path": path, "downstream_branch": "virtual:product-branch",
            "control_valve": "virtual:product-control-valve", "atmospheric_pa": ATM_PA,
            "max_strainer_resistance_pa_s2_m6": float(f"{strainer:.6g}"),
            "max_impeller_head_loss": round(head_loss, 4),
            "max_impeller_efficiency_loss": d.efficiency_loss,
            "max_valve_opening_loss": d.valve_opening_loss,
            "max_booster_strainer_resistance_pa_s2_m6": float(f"{booster_strainer:.6g}"),
            "max_booster_head_loss": round(booster_head_loss, 4),
            "max_booster_efficiency_loss": d.efficiency_loss,
            "station": {"topology": d.topology,
                        "max_second_strainer_resistance_pa_s2_m6": float(f"{second_strainer or strainer:.6g}"),
                        "units": [{"unit_id": u, "pump": pump, "npsh": feed_npsh, **line} for u in (d.source, d.second)]},
            "network": {
                "density_kg_m3": rho, "pump_npsh": feed_npsh,
                "pump": pump,
                "branches": [
                    {"branch_id": "virtual:other-branch", "outlet_node": "virtual:other-boundary", "outlet_abs_pa": p_other,
                     "pipes": {"virtual:other-equivalent": {
                         "length_m": d.other_length, "diameter_m": d_other, "darcy_factor": DARCY,
                         "minor_loss_k": pipe_k(r_other, rho, d.other_length, d_other)}}},
                    {"branch_id": "virtual:product-branch", "outlet_node": "virtual:product-boundary",
                     "outlet_abs_pa": p_tail_out,
                     "pipes": {"virtual:process-equivalent": {
                         "length_m": d.product_length, "diameter_m": d_product, "darcy_factor": DARCY,
                         "minor_loss_k": pipe_k((1 - valve_share) * r_product, rho, d.product_length, d_product)}},
                     "valves": {"virtual:product-control-valve": {"diameter_m": d_product, "full_open_k": valve_k}},
                     # 액위 조절 밸브가 유출 = 유입을 맞춘다. inlet_abs_pa = 정상 후속 흡입압(드럼 압력 + 설정 액위 수두)
                     **({"drum": {"inlet_abs_pa": ps2, "level_valve_share": DRUM_VALVE_SHARE}} if d.drum else {}),
                     "booster": {"speed_rpm": d.booster_speed, "pump": {
                         "reference_speed_rpm": d.booster_speed, "rated_flow_m3_s": q2,
                         "shutoff_head_m": head2 * d.booster_shutoff_ratio, "rated_head_m": head2,
                         "efficiency": d.booster_efficiency},
                         "discharge_pipe": {"length_m": d.tail_length, "diameter_m": d_tail, "darcy_factor": DARCY,
                                            "minor_loss_k": pipe_k(r_tail, rho, d.tail_length, d_tail)},
                         "npsh": booster_npsh}},
                ],
            },
            "default_point": {"suction_abs_pa": ps1, "speed_rpm": d.speed},
        }

    def solve(cfg: dict, **amounts: float):
        return CoupledCase.model_validate(cfg).solve(
            amounts.get("clog", 0), amounts.get("wear", 0), amounts.get("restriction", 0),
            amounts.get("booster_clog", 0), amounts.get("booster_wear", 0),
            amounts.get("second_clog", 0), amounts.get("second_wear", 0))

    def product(result):
        return next(b for b in result.branches if b.branch_id == "virtual:product-branch")

    def outflow(branch) -> float:
        """후속 펌프 유량: 드럼이 있으면 드럼 유출, 없으면 분기 유량."""
        return branch.flow_m3_s if branch.booster_flow_m3_s is None else branch.booster_flow_m3_s

    def gauge(eid: str, signal: str, value_abs: float) -> float:
        return (value_abs - ATM_PA) / PRESSURE_PA[tags[eid][signal].unit]

    def limits(eid: str, signal: str) -> dict:
        lim = tags[eid][signal].limits
        return lim.model_dump(exclude_none=True) if lim else {}

    base = config(0.08, 1.0, 0.10, 1.0, 0.10)
    # 1) 밸브 저항 비중: 최대 제한에서 후속 유량이 하한 목표에 닿게 한다.
    flow_target = lower_target(limits(d.downstream, "flow"), mu(d.downstream, "flow"))
    # 이 직렬 등가 회로의 후속 흡입압은 제품 유량만의 함수(p_out + R_tail·Q² − H₂(Q))라 유량이 줄면 함께 낮아진다.
    # 유량 목표와 후속 흡입압 목표 중 먼저 닿는 비중을 쓴다.
    b_target = lower_target(limits(d.downstream, "suction_pressure"), mu(d.downstream, "suction_pressure"))

    def restricted(s: float):
        return product(solve(config(s, 1, 0.1, 1, 0.1), restriction=1))

    def feasible(s: float) -> bool:
        try:
            restricted(s)
            return True
        except ValueError:
            return False

    upper = bisect(lambda s: 0.0 if feasible(s) else 1.0, 0.001, 0.6, 0.5, increasing=True) * 0.999
    share_flow = bisect(lambda s: restricted(s).flow_m3_s * 3600 if feasible(s) else -1e9,
                        0.001, upper, flow_target, increasing=False)
    share_suction = bisect(lambda s: gauge(d.downstream, "suction_pressure", restricted(s).booster_suction_abs_pa)
                           if feasible(s) else -1e9, 0.001, upper, b_target, increasing=False)
    share_margin = bisect(lambda s: restricted(s).booster_npsh_margin if feasible(s) else -1e9,
                          0.001, upper, MARGIN_FLOOR, increasing=False)
    share = min(share_flow, share_suction, share_margin)
    # 2) 입력 펌프 스트레이너: 최대 막힘에서 흡입압·그 펌프 유량(병렬은 다른 펌프가 받아 가 유량이 먼저 무너진다) 하한 목표 중 먼저 닿는 쪽.
    s_target = lower_target(limits(d.source, "suction_pressure"), mu(d.source, "suction_pressure"))
    strainer = min(
        bisect(lambda r: gauge(d.source, "suction_pressure", solve(config(share, r, 0.1, 1, 0.1), clog=1).suction_abs_pa),
               1.0, 1e14, s_target, increasing=False),
        bisect(lambda r: solve(config(share, r, 0.1, 1, 0.1), clog=1).pump_flow_m3_s * 3600,
               1.0, 1e14, lower_target(limits(d.source, "flow"), mu(d.source, "flow")), increasing=False),
        # 캐비테이션이 먼저 시작되면 여유비 0.85 까지만 (모델 유효 범위 안)
        bisect(lambda r: solve(config(share, r, 0.1, 1, 0.1), clog=1).pump_npsh_margin, 1.0, 1e14, MARGIN_FLOOR, increasing=False))
    # 3) 입력 펌프 마모: 토출압 하한이 있으면 그 목표(양정 감소 15 % 상한), 없으면 10 %(관리 기준 편차로만 보인다).
    d_target = lower_target(limits(d.source, "discharge_pressure"), mu(d.source, "discharge_pressure"))
    head_loss = 0.10 if d_target is None else bisect(
        lambda h: gauge(d.source, "discharge_pressure", solve(config(share, strainer, h, 1, 0.1), wear=1).discharge_abs_pa),
        0.02, 0.15, d_target, increasing=False)
    # 4) 후속 펌프 스트레이너: 후속 유량(CDU 와 같은 기준)·흡입압 하한·NPSH 여유비 중 먼저 닿는 쪽. 캐비테이션이 흡입압을 받쳐
    #    흡입압·여유비 목표에 닿지 않는 회로는 유량 목표가 없으면 탐색 상한(1e14)까지 가서 막힘 10 % 에도 유량이 0 에 가까웠다 (2026-11-25 D3).
    booster_strainer = min(
        bisect(lambda r: outflow(product(solve(config(share, strainer, head_loss, r, 0.1), booster_clog=1))) * 3600,
               1.0, 1e14, flow_target, increasing=False),
        bisect(lambda r: gauge(d.downstream, "suction_pressure", product(
            solve(config(share, strainer, head_loss, r, 0.1), booster_clog=1)).booster_suction_abs_pa),
            1.0, 1e14, b_target, increasing=False),
        bisect(lambda r: product(solve(config(share, strainer, head_loss, r, 0.1), booster_clog=1)).booster_npsh_margin,
               1.0, 1e14, MARGIN_FLOOR, increasing=False))
    # 5) 후속 펌프 마모: 이 직렬 등가 회로에서 후속 토출압은 제품 유량만의 함수라 토출압 목표로 역산할 수 없다.
    #    양정 10 % 감소를 쓰고, 관측은 전류·흡입압의 관리 기준 편차로 나타난다.
    bd_target = None
    booster_head_loss = 0.10
    # 6) 둘째 입력 펌프 막힘. 병렬은 같은 흡입 경계라 첫 펌프와 같다. 직렬 2단은 2단 흡입압 하한·2단 유량 하한·NPSH 여유비 중 먼저 닿는 쪽.
    second_targets = {}
    second_strainer = None
    if series:
        second_targets = {"second_suction": lower_target(limits(d.second, "suction_pressure"), mu(d.second, "suction_pressure")),
                          "second_flow": lower_target(limits(d.second, "flow"), mu(d.second, "flow"))}

        def blocked(r: float):
            return solve(config(share, strainer, head_loss, booster_strainer, booster_head_loss, r), second_clog=1)
        candidates = {
            "second_suction": bisect(lambda r: gauge(d.second, "suction_pressure", blocked(r).units[1].suction_abs_pa),
                                     1.0, 1e14, second_targets["second_suction"], increasing=False),
            "second_flow": bisect(lambda r: blocked(r).units[1].flow_m3_s * 3600, 1.0, 1e14, second_targets["second_flow"], increasing=False),
            "npsh_margin": bisect(lambda r: blocked(r).units[1].npsh_margin, 1.0, 1e14, MARGIN_FLOOR, increasing=False),
            # 후속 펌프도 유효 범위 안에 있어야 한다
            "downstream_npsh_margin": bisect(lambda r: product(blocked(r)).booster_npsh_margin, 1.0, 1e14, MARGIN_FLOOR, increasing=False)}
        second_targets["second_basis"] = min(candidates, key=candidates.get)
        second_strainer = min(candidates.values())
    result = config(share, strainer, head_loss, booster_strainer, booster_head_loss, second_strainer)
    targets = {"product_flow_m3h": flow_target, "valve_share_basis": "flow" if share == share_flow else "downstream_suction", "source_suction": s_target, "source_discharge": d_target,
               "downstream_suction": b_target, "downstream_discharge": bd_target, **second_targets}
    return result, targets


def dump(key: str, cfg: dict, targets: dict) -> str:
    d = DESIGN[key]
    header = [
        f"# synthetic assumption: {cfg['plant_id']} 대표 수력 등가회로 ({d.source}·{d.second} {'직렬' if d.topology == 'series' else '병렬'} → 공통 헤더 → 두 등가 분기, 제품 분기에 {d.downstream}).",
        f"# scripts/calibrate_physics.py {key} 로 생성. 정상 해는 plant_{key}.yaml 의 운전점 μ 를 재현한다.",
        "# 밀도·회전수·효율·체절 비·배관 치수·분기 압력 비율·NPSHr·정상 NPSH 여유비·유체 온도는 합성 설계 선택이며 현장 자료가 아니다.",
        "# 입력 펌프 두 대의 직렬·병렬 관계와 토출 라인(10 m, 2 m/s, 체크밸브 K 2.0, 게이트 K 0.2)도 합성 가정이다(PU-D4·D5a).",
        "# 고장 최대치는 아래 목표(태그 단위)에 닿도록 역산했다. None 은 해당 하한이 마스터에 없어 기본 10 % 를 쓴 항목.",
        *(["# 제품 분기 끝에 후속 펌프 흡입 드럼(탑·드럼 대용모델, 2026-11-26, open-issues 42·43): inlet_abs_pa = 정상 후속 흡입압.",
           "#   액위 조절 밸브(정상점 토출 저항의 50 %, 정상 개도 0.5)가 유출 = 유입을 맞추고, 고장 최대치는 드럼 회로로 역산했다."] if d.drum else []),
        *(f"#   {name}: {value if not isinstance(value, float) else round(value, 4)}" for name, value in targets.items()),
    ]
    return "\n".join(header) + "\n" + yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, width=120)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("plants", nargs="+", choices=sorted(DESIGN))
    parser.add_argument("--write", action="store_true", help="config/physics_<key>_integrated.yaml 에 쓴다")
    args = parser.parse_args()
    for key in args.plants:
        cfg, targets = build(key)
        text = dump(key, cfg, targets)
        if args.write:
            (ROOT / f"config/physics_{key}_integrated.yaml").write_text(text, encoding="utf-8")
            print(f"config/physics_{key}_integrated.yaml")
        else:
            print(text)


if __name__ == "__main__":
    main()
