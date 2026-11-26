"""교호작용 규칙 I1~I6 의 대상·모델 종류와, 같은 시드 2×2 × on/off 대조 실험.

규칙 자체는 여기서 계산하지 않는다. 생성기가 쓰는 기존 경로(구성안 derive, 고장 모드 boost)를 그대로 돌리고,
대조군은 PlantGenerator(interactions_off) 로 교호항만 끈다: 계산식(I1·I2)은 기준 운전점에 고정한 가산식, boost(I3~I6)는 배율 1.
물리 회로가 정하는 태그에는 교호작용 스위치도 합성 효과도 걸리지 않는다. 모든 크기는 synthetic 이다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from common.config_models import Operating, PlantConfig, RuntimeConfig
from common.sim_clock import KST
from simulator.fault import Effect, FaultEvent, FaultMode, Label, Stage
from simulator.model import PlantGenerator, derive_rule

RULES = ("I1", "I2", "I3", "I4", "I5", "I6")
BASE_MODE = {"I3": "PM-SUCTION-LOSS", "I4": "FN-IMPELLER-FOULING", "I5": "CC-SURGE", "I6": "GB-LUBE-WEAR"}
NEEDS = {"I4": ("speed", "vibration", "bearing_temp"), "I5": ("flow", "suction_pressure", "discharge_pressure", "shaft_vibration"),
         "I6": ("oil_pressure", "oil_temp", "vibration", "bearing_temp")}
RESPONSES = {"I1": ("current",), "I2": ("discharge_temp",), "I3": ("vibration", "flow"), "I4": ("vibration", "bearing_temp"),
             "I5": ("flow", "discharge_pressure", "shaft_vibration"), "I6": ("vibration", "bearing_temp")}
CELLS = ("LL", "HL", "LH", "HH")                # (요인 A, 요인 B)
SPEED_LEVELS = (0.92, 1.03)                     # I4 낮음·높음 운전점 (네 팬의 회전수 한계 안)
START = datetime(2026, 11, 12, tzinfo=KST)
MEASURE = (1500, 1800)                          # 사건 시작 뒤 측정 창 (초). 단계·상승이 끝난 뒤이고 I3 은 경고 구간 보장이 풀린 뒤


def targets(rule: str, plant: PlantConfig, modes: dict[str, FaultMode]) -> list[str]:
    """규칙 조건을 만족하는 설비."""
    out = []
    for e in plant.equipment:
        signals = {t.signal for t in e.tags}
        if rule in ("I1", "I2"):
            hit = any(derive_rule(e.type, t) == rule for t in e.tags)
        elif rule == "I3":
            hit = e.equip_id in modes[BASE_MODE[rule]].boost.equip_ids
        else:
            hit = modes[BASE_MODE[rule]].applies_to(e) and set(NEEDS[rule]) <= signals
        if hit:
            out.append(e.equip_id)
    return out


def response_model(rule: str, physical: bool) -> str:
    """반응 태그의 모델 종류. physical 은 그 태그를 물리 회로 해가 정하는가."""
    if physical:
        return "physics_coupled"
    return {"I1": "derived_proxy", "I2": "derived_relation", "I3": "empirical", "I4": "empirical",
            "I5": "synthetic_no_performance_map", "I6": "synthetic_empirical"}[rule]


def coverage(plant: PlantConfig, runtime: RuntimeConfig, modes: dict[str, FaultMode]) -> list[dict]:
    """대상 설비의 반응 태그마다 이 실행 설정에서의 모델 종류와 교호항 스위치 적용 여부."""
    physics = PlantGenerator(plant, runtime).physics
    tags = physics.tag_keys if physics else set()
    by_id = plant.equipment_by_id()
    rows = []
    for rule in RULES:
        for eid in targets(rule, plant, modes):
            for t in by_id[eid].tags:
                if t.signal in RESPONSES[rule] and (rule not in ("I1", "I2") or derive_rule(by_id[eid].type, t) == rule):
                    # 일반 모드: 계산 태그는 입력의 변화를 계산식으로 따르고, I3 는 키운 만큼을 회로 해 위에 더한다
                    physical = (eid, t.tag_id) in tags and not (runtime.physics_general and (t.derive or rule == "I3"))
                    rows.append({"rule": rule, "equip_id": eid, "tag_id": t.tag_id, "signal": t.signal,
                                 "model": response_model(rule, physical), "switchable": not physical})
    return rows


@dataclass(frozen=True)
class _Events:
    items: tuple[FaultEvent, ...]

    def external(self, start: float, end: float) -> list[FaultEvent]:
        return [e for e in self.items if e.start < end]


def _hold(equip_id: str, name: str, effects: list[Effect], start: float) -> FaultEvent:
    """요인 하나를 일정하게 거는 실험용 사건 (고장 모드 파일에는 없다)."""
    mode = FaultMode(id=f"EXP-{name}", name=name, equip_ids=[equip_id], label=Label(kind="실험", cause="요인 수준", action="-", code=1),
                     stages=[Stage(id="F", name="요인", at_sec=0, effects=effects)])
    return FaultEvent(f"EXP-{name}", equip_id, mode, start)


def _only(mode: FaultMode, stages: set[str]) -> FaultMode:
    """기본 고장 모드에서 요인 단계를 뺀 실험용 사본. 요인은 _hold 로 따로 건다."""
    return mode.model_copy(update={"id": f"EXP-{mode.id}", "stages": [s for s in mode.stages if s.id in stages]})


def _relative(e, signal: str, fraction: float) -> list[Effect]:
    return [Effect(signal=signal, shape="step", delta=t.control.mean * fraction) for t in e.tags if t.signal == signal][:1]


def _design(rule: str, plant: PlantConfig, eid: str, modes: dict[str, FaultMode], start: float, high: tuple[bool, bool]):
    """한 칸의 사건과 (필요하면) 운전점을 바꾼 구성안."""
    e, (a, b) = plant.equipment_by_id()[eid], high
    events: list[FaultEvent] = []

    def factor(name, effects):
        events.append(_hold(eid, name, effects, start))

    if rule == "I1":        # 낮음 −4 % / 높음 +4 %. 차압은 토출 압력으로 움직인다
        factor("flow", _relative(e, "flow", 0.04 if a else -0.04))
        factor("dp", _relative(e, "discharge_pressure", 0.04 if b else -0.04))
    elif rule == "I2":      # 흡입 온도 ±5 K, 토출 압력 ±4 %
        factor("suction_temp", [Effect(signal="suction_temp", shape="step", delta=5 if a else -5)])
        factor("ratio", _relative(e, "discharge_pressure", 0.04 if b else -0.04))
    elif rule == "I3":
        if a:
            events.append(FaultEvent("EXP-BASE", eid, modes[BASE_MODE[rule]], start))
        if b:
            factor("process_temp", [Effect(signal="process_temp", shape="step", delta=15)])
    elif rule == "I4":
        if a:
            events.append(FaultEvent("EXP-BASE", eid, modes[BASE_MODE[rule]], start))
        rpm = next(t.control.mean for t in e.tags if t.signal == "speed") * SPEED_LEVELS[b]
        plant = plant.model_copy(update={"equipment": [
            x.model_copy(update={"operating": Operating(speed_rpm=rpm)}) if x.equip_id == eid else x for x in plant.equipment]})
    else:
        base = modes[BASE_MODE[rule]]
        keep = {"I5": {"S2", "S3"}, "I6": {"S2"}}[rule]      # 서지 맥동·진동 / 치면 마모. 요인(저유량·유압·유온) 단계는 뺀다
        events.append(FaultEvent("EXP-BASE", eid, _only(base, keep), start))
        for on, f in zip((a, b), base.boost.factors):
            if on:
                t = next(x for x in e.tags if x.signal == f.signal)
                size = f.span * ((t.control.mean + f.offset) / t.control.sigma if f.over else 1)      # 요인 수준 1
                factor(f.signal, [Effect(signal=f.signal, shape="step", z=f.direction * size)])
    return plant, events


def run_cell(rule: str, plant: PlantConfig, runtime: RuntimeConfig, eid: str, modes: dict[str, FaultMode],
             high: tuple[bool, bool], on: bool, seed: int, window: tuple[int, int] = MEASURE) -> dict[str, np.ndarray]:
    """한 칸의 반응 태그 값. 억제 표집을 꺼서 같은 시드면 모든 칸의 정상 잡음이 같다."""
    start = START + timedelta(hours=1)
    plant, events = _design(rule, plant, eid, modes, start.timestamp(), high)
    runtime = runtime.model_copy(update={"seed": seed, "quiet_normal": False, "units": [plant.equipment_by_id()[eid].unit_id]})
    gen = PlantGenerator(plant, runtime, _Events(tuple(events)), frozenset() if on else frozenset({rule}))
    out: dict[str, list[float]] = {}
    for s in gen.generate(start + timedelta(seconds=window[0]), start + timedelta(seconds=window[1])):
        if s.equip_id == eid:
            out.setdefault(s.tag_id, []).append(s.value)
    return {k: np.array(v) for k, v in out.items()}


def normal_low_flow(plant: PlantConfig, runtime: RuntimeConfig, eid: str, modes: dict[str, FaultMode], seed: int) -> dict[str, np.ndarray]:
    """I5 비교군: 서지와 같은 저유량(요인 A 높음)만 있고 맥동이 없는 정상 저유량."""
    start = START + timedelta(hours=1)
    f = modes[BASE_MODE["I5"]].boost.factors[0]
    ev = _hold(eid, "normal_low_flow", [Effect(signal=f.signal, shape="step", z=f.direction * f.span)], start.timestamp())
    runtime = runtime.model_copy(update={"seed": seed, "quiet_normal": False, "units": [plant.equipment_by_id()[eid].unit_id]})
    out: dict[str, list[float]] = {}
    for s in PlantGenerator(plant, runtime, _Events((ev,))).generate(start + timedelta(seconds=MEASURE[0]), start + timedelta(seconds=MEASURE[1])):
        if s.equip_id == eid:
            out.setdefault(s.tag_id, []).append(s.value)
    return {k: np.array(v) for k, v in out.items()}


def _metric(rule: str, signal: str, values: np.ndarray) -> float:
    """반응 크기. 서지의 유량·토출 압력은 맥동 크기(표준편차), 나머지는 평균."""
    return float(values.std() if rule == "I5" and signal != "shaft_vibration" else values.mean())


def experiment(rule: str, plant: PlantConfig, runtime: RuntimeConfig, eid: str, modes: dict[str, FaultMode],
               seed: int) -> list[dict]:
    """반응 태그마다 2×2 네 칸의 값(on/off)과 교호 대비 (HH − HL) − (LH − LL)."""
    runs = {(c, on): run_cell(rule, plant, runtime, eid, modes, (c[0] == "H", c[1] == "H"), on, seed)
            for c in CELLS for on in (True, False)}
    rows = []
    for r in (x for x in coverage(plant, runtime, modes) if x["rule"] == rule and x["equip_id"] == eid):
        row = r | {"plant_id": plant.plant.plant_id, "seed": seed, "sigma": plant.tag_by_key()[(eid, r["tag_id"])].control.sigma}
        for on in (True, False):
            v = {c: _metric(rule, r["signal"], runs[c, on][r["tag_id"]]) for c in CELLS}
            name = "on" if on else "off"
            row |= {f"{c}_{name}": v[c] for c in CELLS}
            row[f"contrast_{name}"] = (v["HH"] - v["HL"]) - (v["LH"] - v["LL"])
        rows.append(row)
    return rows
