"""PU-D6 CDU 경유 제품 측 합성 완충/분배 대용모델의 시간 계산. 증류탑(AT) 물리 모델이 아니다.

정적 계산(simulator.static_calc, M1)과 구분되는 시간 재생이다. 요청은 M1 변경 목록을 시각별로 나열한 타임라인이고,
매번 정상 운전점·액위 설정값에서 시작해 끝까지 다시 적분한다(무상태, 결정적).

    dV/dt = alpha · Qfeed(t) − Qout(t) − Qoverflow(t)

- Qfeed: 그 시각 구간 M1 정적 계산의 결합 회로 총유량(P-8101A + P-8101B). 입력 펌프 쪽은 준정적(즉시)이다.
- alpha: 정상 Qout / 정상 Qfeed (정상 해에서 계산).
- Qout: P-8401A. 흡입압 = 용기 상부압 + ρg(액위 + 노즐 높이차), 양정·NPSH 는 기존 DemandSide.boost(후속 펌프 곡선),
  토출 쪽 저항 = 기존 토출 배관 저항 중 (1 − share) + 액위 제어밸브. 정상 액위·정상 개도에서 기존 등가회로 정상점과 같다.
- 액위가 하한에 닿으면 유출을 유입까지 줄이고(흡입 상실), 상한에 닿으면 넘친 양을 따로 적는다(둘 다 경계 사건).
P-8401A 의 유량·토출압·전류만 이 모델이 정하고, 나머지 태그는 그 구간 정적 계산 값을 쓴다.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Literal

from pydantic import Field

from common.config_models import StrictModel, load_yaml
from simulator.physics.network import DemandSide
from simulator.physics.pump import GRAVITY_M_S2, Positive
from simulator.physics.valve import Valve
from simulator.physics_adapter import PhysicsAdapter
from simulator.static_calc import ROOT, CalcError, PlantCalc, flow_control_off, stage_of

DYNAMIC_ENGINE = "buffer-v1"


class Vessel(StrictModel):
    area_m2: Positive
    min_level_m: float = Field(ge=0)
    max_level_m: Positive
    setpoint_m: Positive
    nozzle_elevation_m: float = Field(ge=0)


class LevelValve(StrictModel):
    diameter_m: Positive
    share_of_discharge_loss: float = Field(gt=0, lt=1)
    normal_opening: float = Field(gt=0, le=1)


class Control(StrictModel):
    mode: Literal["pi", "manual"]
    gain: float = Field(ge=0)
    integral_sec: Positive
    min_opening: float = Field(gt=0, le=1)
    max_opening: float = Field(gt=0, le=1)


class BufferConfig(StrictModel):
    provenance: Literal["synthetic assumption"]
    plant_id: str
    circuit: str
    node: str
    feeds: list[str] = Field(min_length=1)
    target: str
    vessel: Vessel
    valve: LevelValve
    control: Control
    dt_sec: int = Field(default=1, ge=1)
    max_duration_sec: int = Field(default=7200, ge=1)


class BufferModel:
    """한 공장의 완충 대용모델. 시작할 때 연결 근거(마스터 흐름 간선·회로 경로)와 정상점 일치를 확인한다."""

    def __init__(self, calc: PlantCalc, path: Path) -> None:
        self.calc = calc
        self.path = path
        self.cfg = cfg = BufferConfig.model_validate(load_yaml(path))
        a = calc.adapters.get(cfg.circuit)
        if not isinstance(a, PhysicsAdapter) or cfg.plant_id != calc.plant.plant.plant_id:
            raise ValueError(f"{cfg.circuit}: 결합 회로가 아닙니다.")
        route = a.case.downstream_path
        process = {(x, y) for x, y, _, s in calc.plant.flow.edges if s == "process"}
        # 연결 근거: 회로 경로의 마지막 두 노드가 node → target 이고 마스터 공정 간선에 있으며, 입력 펌프가 회로의 두 입력이다
        if (route[-1] != cfg.target or route[-2] != cfg.node or (cfg.node, cfg.target) not in process
                or set(cfg.feeds) != {a.primary, a.second} or a.downstream != cfg.target):
            raise ValueError("완충 대용모델의 node·target·feeds 가 회로 경로·마스터 공정 간선과 맞지 않습니다.")
        v = cfg.vessel
        if not v.min_level_m < v.setpoint_m < v.max_level_m:
            raise ValueError("액위 하한 < 설정값 < 상한이어야 합니다.")
        if not cfg.control.min_opening <= cfg.valve.normal_opening <= cfg.control.max_opening:
            raise ValueError("정상 개도는 제어 개도 범위 안이어야 합니다.")
        self.adapter = a
        net = a.case.network
        self.rho = net.density_kg_m3
        self.branch = next(b for b in net.branches if b.branch_id == a.case.downstream_branch)
        self.side = DemandSide(net, a.case.default_point, 0.0)          # 후속 펌프 양정·NPSH 식 재사용
        normal = calc._ref_station[a.primary]
        product = next(b for b in normal.branches if b.branch_id == a.case.downstream_branch)
        self.q0 = product.flow_m3_s
        self.qfeed0 = normal.flow_m3_s
        self.alpha = self.q0 / self.qfeed0
        self.shaft0 = product.booster_shaft_w
        self.p_bs0 = product.booster_suction_abs_pa
        self.p_top = self.p_bs0 - self.rho * GRAVITY_M_S2 * (v.setpoint_m + v.nozzle_elevation_m)
        if self.p_top <= 0:
            raise ValueError("용기 상부 절대압이 양수가 되도록 노즐 높이·설정 액위를 정해야 합니다.")
        tail = self.branch.booster.discharge_pipe.resistance(self.rho)
        r_valve0 = cfg.valve.share_of_discharge_loss * tail
        self.r_pipe = tail - r_valve0
        area = math.pi * cfg.valve.diameter_m ** 2 / 4
        self.valve = Valve(diameter_m=cfg.valve.diameter_m,
                           full_open_k=r_valve0 * 2 * (area * cfg.valve.normal_opening) ** 2 / self.rho)
        q, _ = self.outflow(v.setpoint_m, cfg.valve.normal_opening)
        if not math.isclose(q, self.q0, rel_tol=1e-9):
            raise ValueError(f"정상 액위·개도의 P-8401A 유량 {q * 3600:.4f} m3/h 가 등가회로 정상점 {self.q0 * 3600:.4f} 과 다릅니다.")
        keys = a.keys[cfg.target]
        self.keys = {s: keys[s] for s in ("flow", "discharge_pressure", "redundant_discharge_pressure", "current") if s in keys}
        self.centers0 = calc._normal_centers[cfg.circuit]
        text = path.read_bytes()
        import hashlib
        self.version = {"engine": DYNAMIC_ENGINE, "buffer_sha256": hashlib.sha256(text).hexdigest()[:16], **calc.model_version}

    # --- 후속 펌프 ----------------------------------------------------------------------------
    def suction(self, level: float) -> float:
        return self.p_top + self.rho * GRAVITY_M_S2 * (level + self.cfg.vessel.nozzle_elevation_m)

    def outflow(self, level: float, opening: float) -> tuple[float, dict]:
        """액위·개도 → (P-8401A 유량 m3/s, 상태). 기존 후속 펌프 식(DemandSide.boost)으로 푼다."""
        p = self.suction(level)
        b = self.branch.booster
        r = self.r_pipe + self.valve.resistance(self.rho, opening)
        outlet = self.branch.outlet_abs_pa

        def excess(q: float) -> float:
            return p + self.side.boost(self.branch, q, p)[0] - r * q * q - outlet

        hi = math.sqrt(b.pump.shutoff_at(b.speed_rpm) / b.pump.curvature)
        if excess(0.0) <= 0:
            raise CalcError("no_solution", "P-8401A 체절 양정이 출구 경계를 넘지 못합니다.", reason="no_solution")
        lo = 0.0
        for _ in range(80):
            mid = (lo + hi) / 2
            if excess(mid) > 0:
                lo = mid
            else:
                hi = mid
        q = (lo + hi) / 2
        dp, loss = self.side.boost(self.branch, q, p)
        if b.npsh is not None:
            _, npsha, _, _, cav = b.npsh.evaluate(q, p, self.rho, b.speed_rpm / b.pump.reference_speed_rpm, b.pump.rated_flow_m3_s)
            if npsha <= 0 or cav >= b.npsh.max_head_loss:
                raise CalcError("no_solution", f"P-8401A 캐비테이션 모델 유효 범위 밖(NPSHa {npsha:.2f} m)", reason="cavitation_range")
        eta = b.pump.efficiency * (1 - (b.npsh.efficiency_coupling if b.npsh else 0.0) * loss)
        return q, {"suction_abs_pa": p, "discharge_abs_pa": p + dp, "shaft_w": q * dp / eta}

    def tag_values(self, q: float, state: dict) -> dict:
        """엔진 규칙 μ + (해 − 정상 해)."""
        a, out = self.adapter, {}
        for signal, key in self.keys.items():
            if signal == "flow":
                center = q * 3600
            elif signal in ("discharge_pressure", "redundant_discharge_pressure"):   # 이중화 전송기도 같은 지점
                center = a._gauge(key, state["discharge_abs_pa"])
            else:
                center = self.calc.control[key][0] * state["shaft_w"] / self.shaft0
            out[key] = self.calc.control[key][0] + (center - self.centers0[key])
        return out

    # --- 시간 재생 ------------------------------------------------------------------------------
    def describe(self) -> dict:
        v, c = self.cfg.vessel, self.cfg.control
        hold = v.area_m2 * v.setpoint_m
        return {"provenance": self.cfg.provenance, "model": "synthetic product-side buffer (not a distillation column model)",
                "node": self.cfg.node, "feeds": self.cfg.feeds, "target": self.cfg.target, "alpha": self.alpha,
                "alpha_basis": "정상 P-8401A 유량 / 정상 입력 펌프 합계 유량",
                "normal_feed_m3_h": self.qfeed0 * 3600, "normal_out_m3_h": self.q0 * 3600,
                "vessel": v.model_dump(), "normal_holdup_m3": hold, "residence_time_min": hold / (self.q0 * 3600) * 60,
                "top_abs_pa": self.p_top, "level_to_suction": "흡입 절대압 = 상부압 + ρg(액위 + 노즐 높이차)",
                "valve": {**self.cfg.valve.model_dump(), "full_open_k": self.valve.full_open_k},
                "control": c.model_dump(), "dt_sec": self.cfg.dt_sec, "tags": [k[1] for k in self.keys.values()]}

    def simulate(self, request: dict) -> dict:
        if not isinstance(request, dict) or set(request) - {"timeline", "duration_sec", "sample_sec"}:
            raise CalcError("input_error", "요청은 timeline·duration_sec·sample_sec 만 받습니다.")
        dt = self.cfg.dt_sec
        duration = request.get("duration_sec", 3600)
        sample = request.get("sample_sec", 10)
        for name, value in (("duration_sec", duration), ("sample_sec", sample)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise CalcError("input_error", f"{name} 는 양의 정수여야 합니다.")
        if duration > self.cfg.max_duration_sec:
            raise CalcError("out_of_range", f"duration_sec 는 {self.cfg.max_duration_sec} 이하여야 합니다.")
        if duration % dt or sample % dt:
            raise CalcError("input_error", f"duration_sec·sample_sec 는 {dt} 초의 배수여야 합니다.")
        timeline = request.get("timeline") or []
        if not isinstance(timeline, list) or len(timeline) > 32:
            raise CalcError("input_error", "timeline 은 32개 이하 배열이어야 합니다.")
        segments, last = [], -1
        for i, seg in enumerate(timeline):
            if not isinstance(seg, dict) or set(seg) - {"at_sec", "changes"} or "at_sec" not in seg:
                raise CalcError("input_error", f"timeline[{i}] 는 at_sec·changes 객체여야 합니다.")
            at = seg["at_sec"]
            if isinstance(at, bool) or not isinstance(at, int) or not last < at < duration or at % dt:
                raise CalcError("input_error", f"timeline[{i}].at_sec 는 증가하는 0 이상 duration 미만의 {dt} 초 배수여야 합니다.")
            last = at
            changes = seg.get("changes") or {}
            self._check_scope(changes, i)
            segments.append((at, changes))
        if not segments or segments[0][0] != 0:
            segments.insert(0, (0, {}))
        # 구간별 정적 계산 (입력 펌프 쪽 준정적, 같은 변경은 한 번만)
        static = []
        cache: dict[str, dict] = {}
        for at, changes in segments:
            key = json.dumps(changes, sort_keys=True)
            if key not in cache:
                with flow_control_off():     # D6 는 자체 액위·제어 모델이 있어 정적 쪽은 개방 회로(유량 제어 가정 없음)로 받는다
                    cache[key] = self.calc.calculate(changes)
            res = cache[key]
            bal = next((b for b in res["circuits"] if b["circuit_id"] == self.cfg.circuit), None)
            qfeed = (bal["total_flow_m3_h"] / 3600) if bal else self.qfeed0
            static.append((at, changes, res, qfeed))
        return self._run(static, duration, sample)

    def _check_scope(self, changes: dict, i: int) -> None:
        """후속 펌프(P-8401A)와 그 태그를 직접 바꾸는 변경은 이 대용모델이 계산하지 않는다(정적 계산에서만 지원)."""
        if not isinstance(changes, dict):
            raise CalcError("input_error", f"timeline[{i}].changes 는 M1 변경 목록 객체여야 합니다.")
        t = self.cfg.target
        hit = ([c for c in changes.get("causes") or [] if isinstance(c, dict) and str(c.get("parameter", "")).startswith(f"{t}/")]
               + [f for f in changes.get("faults") or [] if isinstance(f, dict) and f.get("equip_id") == t]
               + [o for o in changes.get("sensor_overrides") or [] if isinstance(o, dict) and o.get("equip_id") == t])
        if hit:
            raise CalcError("unsupported", f"timeline[{i}]: {t} 를 직접 바꾸는 변경은 시간 계산에서 지원하지 않습니다(정적 계산만).")

    def _run(self, static, duration: int, sample: int) -> dict:
        cfg, v, c = self.cfg, self.cfg.vessel, self.cfg.control
        dt, area, span = cfg.dt_sec, v.area_m2, v.max_level_m - v.min_level_m
        vol = area * v.setpoint_m
        vol0, vmin, vmax = vol, area * v.min_level_m, area * v.max_level_m
        integral, opening = 0.0, cfg.valve.normal_opening
        inflow_total = out_total = over_total = 0.0
        seg_index, samples, events, segs = 0, [], [], []
        stages: dict[tuple, str] = {}
        boundary = None
        seg_stats = None

        def judge(t: int, values: dict) -> None:
            for key, value in values.items():
                st = None if value is None else stage_of(value, self.calc.tags[key].limits)
                prev = stages.get(key, "normal")
                if st is not None and st != prev:
                    events.append({"t_sec": t, "kind": "stage", "equip_id": key[0], "tag_id": key[1], "from": prev, "to": st,
                                   "value": value})
                    stages[key] = st

        for step in range(duration // dt + 1):
            t = step * dt
            while seg_index + 1 < len(static) and static[seg_index + 1][0] <= t:
                seg_index += 1
            at, changes, res, qfeed = static[seg_index]
            if seg_stats is None or seg_stats["at_sec"] != at:
                if seg_stats is not None:
                    segs.append(seg_stats)
                instant = {(x["equip_id"], x["tag_id"]): x["value"] for x in res["tags"] if (x["equip_id"], x["tag_id"]) in self.keys.values()}
                seg_stats = {"at_sec": at, "changes": changes, "feed_m3_h": qfeed * 3600, "product_in_m3_h": self.alpha * qfeed * 3600,
                             "instant": {k[1]: instant.get(k) for k in self.keys.values()},
                             "dynamic_min": {}, "dynamic_max": {}, "dynamic_end": {}}
            level = vol / area
            if c.mode == "pi":
                err = (level - v.setpoint_m) / span
                opening = cfg.valve.normal_opening + c.gain * (err + integral / c.integral_sec)
                clamped = min(c.max_opening, max(c.min_opening, opening))
                if clamped == opening:      # 적분 포화 방지: 개도가 한계에 닿으면 적분을 멈춘다
                    integral += err * dt
                opening = clamped
            q, state = self.outflow(level, opening)
            qin = self.alpha * qfeed
            starved = False
            if vol + (qin - q) * dt < vmin:            # 흡입 상실: 유출을 유입 + 남은 여유까지 줄인다
                q = max(0.0, qin + (vol - vmin) / dt)
                starved = True
            over = max(0.0, vol + (qin - q) * dt - vmax) / dt
            if (starved or over) and boundary != ("low" if starved else "high"):
                boundary = "low" if starved else "high"
                events.append({"t_sec": t, "kind": "boundary", "boundary": "low_level" if starved else "overflow",
                               "text": "액위 하한: P-8401A 흡입 상실로 보고 유출을 유입까지 줄였습니다." if starved
                               else "액위 상한: 넘친 양을 따로 적었습니다(실제로는 공급 차단·우회 필요)."})
            elif not (starved or over):
                boundary = None
            values = {key: None for key in self.keys.values()} if starved else self.tag_values(q, state)
            upstream = {(x["equip_id"], x["tag_id"]): x["value"] for x in res["tags"]
                        if x["equip_id"] in cfg.feeds and x["signal"] in ("flow", "discharge_pressure", "current", "suction_pressure")}
            judge(t, {**upstream, **values})
            for key, value in values.items():
                if value is None:
                    continue
                name = key[1]
                seg_stats["dynamic_min"][name] = min(seg_stats["dynamic_min"].get(name, value), value)
                seg_stats["dynamic_max"][name] = max(seg_stats["dynamic_max"].get(name, value), value)
                seg_stats["dynamic_end"][name] = value
            if t % sample == 0:
                samples.append({"t_sec": t, "level_m": level, "volume_m3": vol, "valve_opening": opening,
                                "feed_m3_h": qfeed * 3600, "product_in_m3_h": qin * 3600, "out_m3_h": q * 3600,
                                "overflow_m3_h": over * 3600, "suction_abs_pa": state["suction_abs_pa"], "starved": starved,
                                "tags": {k[1]: val for k, val in values.items()},
                                "upstream": {k[1]: val for k, val in upstream.items()}})
            if step == duration // dt:
                break
            vol += (qin - q - over) * dt
            inflow_total += qin * dt
            out_total += q * dt
            over_total += over * dt
        segs.append(seg_stats)
        residual = (vol - vol0) - (inflow_total - out_total - over_total)
        return {
            "mode": "dynamic", "synthetic": True, "plant": self.calc.key, "plant_id": self.calc.plant.plant.plant_id,
            "model_version": self.version, "buffer": self.describe(),
            "assumptions": [
                {"kind": "synthetic_buffer", "text": "AT 이후 경유 제품 측 합성 완충 용기다. 증류탑 물리·분리 계산이 아니며 액위는 관측 태그가 없다."},
                {"kind": "fixed_split", "text": "제품 유입 = alpha × 입력 펌프 합계 유량(고정 분배). 공급 조성·온도에 따른 수율 변화는 계산하지 않는다."},
                {"kind": "quasi_static_feed", "text": "입력 펌프 쪽(헤더·유량·전류)은 구간마다 정적 해로 즉시 바뀐다. 펌프 기동·정지 과도는 없다."},
                {"kind": "static_distinct", "text": "이 결과는 시간 재생(mode=dynamic)이다. M1 정적 계산(즉시 등가 전파)과 섞지 않는다."},
            ],
            "segments": segs, "events": events, "samples": samples,
            "balance": {"initial_volume_m3": vol0, "final_volume_m3": vol, "inflow_m3": inflow_total, "outflow_m3": out_total,
                        "overflow_m3": over_total, "residual_m3": residual},
        }


class DynamicRegistry:
    """완충 설정이 있는 공장만 시간 재생을 지원한다(현재 CDU)."""

    def __init__(self, registry, config: Path = ROOT / "config") -> None:
        self.models = {}
        for key, calc in registry.calcs.items():
            path = config / f"dynamic_{key}_buffer.yaml"
            if path.exists():
                self.models[key] = BufferModel(calc, path)

    def get(self, key: str) -> BufferModel | None:
        return self.models.get(key)


class LiveBuffer:
    """일반 모드 실시간 생성기에 붙이는 같은 완충 모델 (runtime `dynamic_buffer`, 기본 꺼짐. 2026-11-27 D13 보강).

    시간 재생(_run)과 같은 식(PI 액위 제어·흡입 상실·넘침)을 1초씩 적분하되, 상태(보유량·적분)를 생성 창 사이에 이어 간다.
    입력 유량은 실시간 회로 해의 입력 펌프 두 대 합계이고, 제품 유입 = 정상 유출 × (입력 합계 / 실시간 정상 입력 합계)
    (= alpha × 입력, 실시간 정상점에서 정확히 정상 유출)이다. P-8401A 유량·토출압·전류 중심값만 이 모델 값으로 바꾼다.
    - 이어지지 않는 시각(되돌아감·한 시간 넘는 공백)을 받으면 정상 액위에서 다시 시작한다(재기동·재생성).
    - 정상 액위·정상 유입이면 적분을 건너뛴다(장기 백필 비용).
    한계: 실시간 정답지(answer key)는 완충을 거친 P-8401A 변화를 따로 적지 않는다. 정적 계산(M1)·AI 물리 검증은 완충을 모른다.
    """

    MAX_GAP_SEC = 3600

    def __init__(self, path: Path, plant_key: str, qfeed_normal_m3_s: float) -> None:
        self.model = BufferModel(PlantCalc(plant_key, config=path.parent), path)
        if self.model.cfg.dt_sec != 1:
            raise ValueError("실시간 완충은 dt_sec = 1 만 지원합니다.")
        self.qfeed_normal = qfeed_normal_m3_s
        self.keys = list(self.model.keys.values())
        self.reset()

    def reset(self) -> None:
        cfg = self.model.cfg
        self.vol = cfg.vessel.area_m2 * cfg.vessel.setpoint_m
        self.integral, self.opening, self.last = 0.0, cfg.valve.normal_opening, None
        self.steady = True

    def advance(self, epoch: int, qfeed_m3_s: float) -> dict:
        """epoch 까지 적분하고 그 시각의 P-8401A 중심값(엔진 규칙 μ + 변화량)을 돌려준다."""
        if self.last is None or epoch <= self.last or epoch - self.last > self.MAX_GAP_SEC:
            self.reset()
            self.last = epoch
        m, cfg = self.model, self.model.cfg
        v, c = cfg.vessel, cfg.control
        area, span = v.area_m2, v.max_level_m - v.min_level_m
        qin = m.q0 * qfeed_m3_s / self.qfeed_normal
        if self.steady and qin == m.q0:
            self.last = epoch
            return {}
        self.steady = False
        while self.last < epoch:
            q, _ = self._control_and_flow()
            vmin, vmax = area * v.min_level_m, area * v.max_level_m
            if self.vol + (qin - q) < vmin:
                q = max(0.0, qin + (self.vol - vmin))
            over = max(0.0, self.vol + (qin - q) - vmax)
            self.vol += qin - q - over
            self.last += 1
        q, state = self._control_and_flow(update=False)
        level = self.vol / area
        if qin == m.q0 and abs(level - v.setpoint_m) < 1e-6 and abs(self.integral) < 1e-3:
            self.reset()
            self.last = epoch
        return m.tag_values(q, state)

    def _control_and_flow(self, update: bool = True) -> tuple[float, dict]:
        cfg = self.model.cfg
        v, c = cfg.vessel, cfg.control
        level = max(self.vol / v.area_m2, v.min_level_m)
        opening = self.opening
        if c.mode == "pi":
            err = (level - v.setpoint_m) / (v.max_level_m - v.min_level_m)
            opening = cfg.valve.normal_opening + c.gain * (err + self.integral / c.integral_sec)
            clamped = min(c.max_opening, max(c.min_opening, opening))
            if update and clamped == opening:      # 적분 포화 방지 (_run 과 같다)
                self.integral += err
            opening = clamped
        if update:
            self.opening = opening
        return self.model.outflow(level, opening)
