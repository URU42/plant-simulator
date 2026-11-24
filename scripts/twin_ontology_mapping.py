"""D1: 다섯 플랜트 물리 회로의 설비·태그·공정 관계를 마스터와 진단 저장소 온톨로지·3D 모델에 대조한다.

대조 원본
- 시뮬레이터: config/plant_<plant>.yaml (설비·태그·단위·주기·공정 간선), config/runtime_<plant>.yaml (주기),
  config/physics_<plant>_twin.yaml (회로 묶음·제외 사유)
- 진단 저장소: ontology/source.sql (asset·sensor_tag·topology_edge), ontology/simulator-sensor-tags.csv,
  apps/frontend/public/refinery-model.js (3D 노드·태그·간선)

`--check` 는 불일치가 하나라도 있으면 종료 코드 1. `--output` 에 Markdown 매핑표를 쓴다(새 파일).
실행: `uv run --locked python -m scripts.twin_ontology_mapping --diag ../sk-clx-diag-agent --output docs/twin-ontology-mapping.md --check`
"""
from __future__ import annotations

import argparse
import ast
import csv
import json
import re
from pathlib import Path

from common.config_models import load_plant, load_runtime
from simulator.physics_adapter import PhysicsAdapter, PhysicsBundle, load_physics
from simulator.physics_single import SingleAdapter

ROOT = Path(__file__).resolve().parents[1]
PLANTS = ("cdu", "hou", "rfcc", "rhds", "vrds")
QUANTITY = {"suction_pressure": "흡입 정압(게이지)", "discharge_pressure": "토출 정압(게이지)", "flow": "체적 유량",
            "current": "모터 전류(축동력 비례 대용)", "differential_pressure": "팬 전후 차압"}
SI = {"kg/cm2(g)": "Pa(abs) = v·98,066.5 + 101,325", "bar(g)": "Pa(abs) = v·100,000 + 101,325",
      "m3/h": "m³/s = v/3,600", "Nm3/h": "m³/s = v·ρN/ρ/3,600 (ρN 1.2931 kg/Nm³)", "A": "축동력 비 × 정상 전류",
      "mmH2O": "Pa = v·9.80665"}


def sql_rows(sql: str, table: str) -> list[list[str]]:
    """INSERT 문의 한 줄 한 행 튜플을 파이썬 리터럴로 읽는다(작은따옴표 문자열·숫자·NULL)."""
    start = sql.index(f"INSERT INTO clx_ontology.{table} VALUES")
    end = sql.index(";\n", start)
    rows = []
    for line in sql[start:end + 1].splitlines()[1:]:
        line = line.strip().rstrip(",;")
        if line.startswith("("):
            value = ast.literal_eval(re.sub(r"\bNULL\b", "None", line.replace("\\'", "'")))
            rows.append([str(v) if v is not None else "" for v in value])
    return rows


def refinery_model(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    start = text.index("window.refineryModel = ") + len("window.refineryModel = ")
    body = text[start:text.index("\n};\n", start) + 2]      # 모델 객체만 (뒤의 함수 정의 제외)
    return json.loads(body)


def build(diag: Path) -> tuple[list[str], list[str]]:
    sql = (diag / "ontology/source.sql").read_text(encoding="utf-8")
    assets = {r[0]: r for r in sql_rows(sql, "asset")}
    sensors = {r[0]: r for r in sql_rows(sql, "sensor_tag")}
    edges = {(r[3], r[4], r[5]) for r in sql_rows(sql, "topology_edge")}
    edge_ids = {(r[3], r[4]): r[0] for r in sql_rows(sql, "topology_edge")}
    with (diag / "ontology/simulator-sensor-tags.csv").open(encoding="utf-8") as f:
        csv_rows = {(r["plant_id"], r["equip_id"], r["tag_id"]): r for r in csv.DictReader(f)}
    model = refinery_model(diag / "apps/frontend/public/refinery-model.js")
    problems: list[str] = []
    out = ["# 플랜트 물리 회로 ↔ 온톨로지·3D 매핑 (D1)", "",
           "작성 기준일 2026-11-24 · `scripts/twin_ontology_mapping.py` 생성. AI 편집 정리본이며 팀 승인 문서가 아니다. "
           "키는 원본 그대로 쓴다: 설비 `plant_id/equip_id`, 태그 `plant_id/equip_id/tag_code`. 물리 파라미터는 synthetic assumption 이다.", "",
           "| 플랜트 | 대상 Unit | 회로 | 설비 | 물리 태그 | 제외 설비 |", "| --- | --- | --- | --- | --- | --- |"]
    detail: list[str] = []
    for key in PLANTS:
        plant = load_plant(ROOT / f"config/plant_{key}.yaml")
        runtime = load_runtime(ROOT / f"config/runtime_{key}.yaml")
        pid = plant.plant.plant_id
        bundle = load_physics(plant, ROOT / f"config/physics_{key}_twin.yaml")
        adapters = bundle.adapters if isinstance(bundle, PhysicsBundle) else [bundle]
        equipment = plant.equipment_by_id()
        tags = {(e.equip_id, t.tag_id): t for e in plant.equipment for t in e.tags}
        nodes = model[key.upper()]["nodes"]
        model_edges = {(a, b, kind) for a, b, _, kind in model[key.upper()]["edges"]}
        circuits = bundle.circuits()
        for c in circuits:
            units = sorted({equipment[e].unit_id for e in c["equipment"]})
            ntags = sum(1 for k in bundle.tag_keys if k[0] in c["equipment"])
            out.append(f"| {key.upper()} | {', '.join(units)} | `{c['id']}` {' → '.join(c['path'])} | {', '.join(c['equipment'])} | {ntags} | "
                       + (", ".join(sorted(bundle.exclusions)) if c is circuits[0] else "") + " |")
        detail += [f"## {key.upper()} ({pid})", "", "| 설비 키 | 태그 키 | 물리량 | 단위 (YAML·SQL·CSV·3D) | SI 변환 | 주기 s (YAML·CSV) | 회로·역할 |",
                   "| --- | --- | --- | --- | --- | --- | --- |"]
        for adapter in adapters:
            for (eid, tag_id) in sorted(adapter.tag_keys):
                t = tags[(eid, tag_id)]
                akey, tkey = f"{pid}/{eid}", f"{pid}/{eid}/{tag_id}"
                srow, crow = sensors.get(tkey), csv_rows.get((pid, eid, tag_id))
                node = nodes.get(eid)
                msensor = next((s for s in (node or {}).get("sensors", []) if s["id"] == tag_id), None)
                interval = plant.tag_interval_sec((eid, tag_id), runtime.sample_interval_sec)
                if akey not in assets:
                    problems.append(f"{akey}: asset 없음")
                if srow is None or srow[3] != t.signal or srow[5] != t.unit or int(srow[6]) != interval:
                    problems.append(f"{tkey}: sensor_tag 불일치 {srow}")
                if crow is None or crow["signal"] != t.signal or crow["unit"] != t.unit or int(crow["interval_sec"]) != interval:
                    problems.append(f"{tkey}: CSV 불일치 {crow}")
                if node is None or tag_id not in node.get("tags", []) or (msensor and msensor["unit"] != t.unit):
                    problems.append(f"{tkey}: 3D 모델 노드·태그·단위 불일치")
                role = adapter.roles[eid]
                detail.append(f"| `{akey}` | `{tkey}` | {QUANTITY.get(t.signal, t.signal)} | {t.unit} · {srow and srow[5]} · "
                              f"{crow and crow['unit']} · {msensor and msensor['unit']} | {SI.get(t.unit, '')} | "
                              f"{interval} · {crow and crow['interval_sec']} | `{adapter.circuit_of(eid)}` {role} |")
            if isinstance(adapter, PhysicsAdapter):
                path = adapter.case.downstream_path
                for a, b in zip(path, path[1:]):
                    if (a, b, "process") not in edges:
                        problems.append(f"{pid} {a}→{b}: topology_edge(process) 없음")
                    if (a, b, "process") not in model_edges:
                        problems.append(f"{pid} {a}→{b}: 3D 간선 없음")
                detail.append("")
                detail.append(f"공정 관계 `{adapter.circuit_of(adapter.primary)}`: " + " → ".join(
                    f"{a}→{b} (`{edge_ids.get((a, b), '없음')}`)" for a, b in zip(path, path[1:])))
                detail.append("가상 요소(온톨로지에 없는 계산 전용 키): " + ", ".join(sorted(
                    {adapter.case.suction_node, adapter.case.discharge_node, adapter.case.downstream_branch, adapter.case.control_valve}
                    | {b.branch_id for b in adapter.case.network.branches}
                    | {k for b in adapter.case.network.branches for k in b.pipes})))
            elif isinstance(adapter, SingleAdapter):
                case = adapter.case
                for node_key in (case.suction_node, case.discharge_node):
                    if not node_key.startswith("virtual:") and node_key not in nodes:
                        problems.append(f"{pid} {node_key}: 3D 노드 없음")
                detail.append("")
                detail.append(f"공정 관계 `{adapter.circuit_id}`: {case.suction_node} → {case.equip_id} → {case.discharge_node} "
                              f"(경계: {case.suction_node}·{case.discharge_node}, 온톨로지 간선 "
                              f"`{edge_ids.get((case.equip_id, case.discharge_node)) or edge_ids.get((case.suction_node, case.equip_id)) or '없음(virtual 경계)'}`)")
            detail.append("")
        detail += ["제외 설비와 사유:", ""] + [f"- `{pid}/{eid}`: {reason}" for eid, reason in sorted(bundle.exclusions.items())] + [""]
        for eid in equipment:
            if eid not in bundle.roles and eid not in bundle.exclusions:
                problems.append(f"{pid}/{eid}: 회로에도 제외 목록에도 없음")
    out += ["", "모든 압력은 내부 계산에서 절대압 Pa, 유량 m³/s 다. 가상 요소는 `virtual:` 키로만 쓰고 온톨로지·센서로 만들지 않는다.", ""]
    out += detail
    out += ["## 대조 결과", "", "불일치 없음." if not problems else "\n".join(f"- {p}" for p in problems)]
    return out, problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--diag", type=Path, default=ROOT.parent / "sk-clx-diag-agent")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    lines, problems = build(args.diag)
    if args.output:
        args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for problem in problems:
        print("MISMATCH", problem)
    print(f"{'PASS' if not problems else 'FAIL'}: {len(problems)} 불일치")
    raise SystemExit(1 if args.check and problems else 0)


if __name__ == "__main__":
    main()
