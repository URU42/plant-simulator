"""디지털 트윈 조작 카탈로그: 설비마다 트윈에서 주입·정지·수리할 수 있는 시나리오와 그 근거.

주입 가능 = 그 설비가 플랜트 대표 수력 회로(입력·후속 펌프)에 있고, 시나리오가 그 역할의 물리 파라미터를 움직인다.
회로 밖 설비의 기존 경험 신호 모드와 회로 설비의 경험 신호 전용 모드는 목록에 남기되 주입할 수 없다고 사유와 함께 표시한다.
정답 분류(label)·기대값(expect)·태그별 효과 계획은 싣지 않는다. 조작 상태(진행·회복·손상)와 운전자가 고른 모드 ID 만 싣는다.
"""
from __future__ import annotations

from collector.anomaly_book import AnomalyBook
from common.config_models import PlantConfig
from simulator.fault import RECOVERY_SEC
from simulator.physics_adapter import PhysicsAdapter

OUTSIDE = "대표 수력 회로 밖 설비: 물리 계산이 없고 경험 신호 모드는 이 트윈의 검증 범위가 아니어서 주입하지 않습니다."
EMPIRICAL = "물리 파라미터가 없는 경험 신호 모드라 이 트윈에서 주입하지 않습니다."
NONE = "이 설비에 적용할 수 있는 시나리오가 없습니다."
CIRCUIT_BUSY = "같은 수력 회로에서 {equip_id}의 시나리오가 진행·회복·손상 중입니다. 정지·수리 후 회복이 끝나면 주입할 수 있습니다."


def _state(book: AnomalyBook, equip_id: str, now: float) -> tuple[str, str | None]:
    """조작 상태와 진행 중 모드. 정답 분류는 쓰지 않는다."""
    events = [e for e in book._events.values() if e.equip_id == equip_id]
    live = next((e for e in events if e.stop is None), None)
    if live is not None:
        return "active", live.mode.id
    damaged = next((e for e in book.damaged() if e.equip_id == equip_id), None)
    if damaged is not None:
        return "damaged", damaged.mode.id
    recovering = [e for e in events if (e.stop is not None and e.stop + RECOVERY_SEC > now)
                  or (e.repair is not None and e.repair + RECOVERY_SEC > now)]
    if recovering:
        return "recovering", recovering[-1].mode.id
    return "idle", None


def _basis(plant: PlantConfig, book: AnomalyBook, physics, mode, equip_id: str, circuit: str | None) -> dict:
    """시나리오 효과의 근거를 셋으로 나눈다(태그 ID 만, 크기·정답 분류 없음).
    physical: 회로 해가 정하는 태그(같은 회로를 다시 풀어 후속 설비까지), empirical: 기존 합성 시나리오 효과(z) 태그,
    ontology_links: 공정·보조 간선으로 닿지만 물리 회로 밖이라 계산으로 전파하지 않는 노드."""
    tags = book._plan(mode, equip_id)
    physical = sorted(f"{k[0]}/{k[1]}" for k, tp in tags.items() if tp.via == "physics")
    empirical = sorted(f"{k[0]}/{k[1]}" for k, tp in tags.items() if tp.via != "physics")
    path_edges: set[tuple[str, str]] = set()
    if physics is not None and circuit:
        path = next(c for c in physics.circuits() if c["id"] == circuit)["path"]
        path_edges = set(zip(path, path[1:]))
    # 회로 경로에 들어 있지 않은 간선은 연결만 표시하고 물리로 전파하지 않는다(예: HOU P-2701 → MIX 재순환).
    links = sorted({b if a == equip_id else a for a, b, _, _ in plant.flow.edges
                    if equip_id in (a, b) and (a, b) not in path_edges})
    return {"physical_tags": physical, "empirical_tags": empirical, "ontology_links": links}


def build_catalog(plant: PlantConfig, book: AnomalyBook, physics: PhysicsAdapter | None, now: float) -> dict:
    excluded: dict[str, str] = getattr(physics, "exclusions", {}) if physics is not None else {}
    notes: dict[str, str] = getattr(physics, "scenario_exclusions", {}) if physics is not None else {}
    blockers: dict[str, str] = {}
    if physics is not None:
        for e in book._events.values():
            circuit = physics.circuit_of(e.equip_id)
            if circuit and circuit not in blockers and book._physical_effect_at(e, now):
                blockers[circuit] = e.equip_id
    equipment = []
    for e in plant.equipment:
        role = physics.roles.get(e.equip_id) if physics else None
        circuit = physics.circuit_of(e.equip_id) if physics else None
        circuit_blocker = blockers.get(circuit) if circuit else None
        state, active_mode = _state(book, e.equip_id, now)
        scenarios = []
        for mode in book.applicable(e.equip_id):
            physical = bool(physics and physics.physical_parameters(mode, e.equip_id))
            reason, blocked = None, None
            if not physical:
                note = notes.get(f"{e.equip_id}/{mode.id}")
                reason, blocked = (note or (EMPIRICAL if role else excluded.get(e.equip_id, OUTSIDE))), "unsupported"
            elif state == "active":
                reason, blocked = "이 설비에 진행 중인 시나리오가 있습니다. 먼저 정지하세요.", "equipment_active"
            elif circuit_blocker is not None:
                reason, blocked = CIRCUIT_BUSY.format(equip_id=circuit_blocker), "circuit_busy"
            scenarios.append({
                "mode": mode.id, "name": mode.name, "model": "physical" if physical else "empirical",
                "supported": physical, "injectable": reason is None, "blocked_by": blocked, "reason": reason,
                "damage": any(s.damage for s in mode.stages),
                "basis": _basis(plant, book, physics, mode, e.equip_id, circuit),
            })
        supported = [s for s in scenarios if s["supported"]]
        equipment.append({
            "equip_id": e.equip_id, "name": e.name, "unit_id": e.unit_id, "type": e.type,
            "hydraulic_role": role, "circuit_id": circuit, "selectable": True, "state": state, "active_mode": active_mode,
            "can_stop": state == "active", "can_repair": state == "damaged",
            "scenarios": scenarios,
            "reason": None if supported else (NONE if not scenarios else (excluded.get(e.equip_id, OUTSIDE) if role is None else EMPIRICAL)),
        })
    return {
        "plant_id": plant.plant.plant_id,
        # circuit: 첫 결합 회로(기존 필드, 호환). circuits: 이 공장의 모든 독립 물리 회로.
        "circuit": None if physics is None else {
            "source": physics.primary, "downstream": physics.downstream, "path": physics.case.downstream_path,
            "unit_id": physics.case.unit_id, "provenance": physics.case.provenance,
        },
        "circuits": [] if physics is None else physics.circuits(),
        "equipment": equipment,
    }


def injectable(catalog: dict, equip_id: str, mode: str) -> tuple[int, str | None]:
    """주입 가능하면 (200, None). 지원하지 않는 조합은 422, 진행·회복 중이라 지금만 막힌 조합은 409."""
    asset = next((a for a in catalog["equipment"] if a["equip_id"] == equip_id), None)
    if asset is None:
        return 404, f"unknown equipment {equip_id}"
    scenario = next((s for s in asset["scenarios"] if s["mode"] == mode), None)
    if scenario is None:
        return 422, f"{mode} is not a scenario for {equip_id}"
    if scenario["injectable"]:
        return 200, None
    return (422 if scenario["blocked_by"] == "unsupported" else 409), scenario["reason"]
