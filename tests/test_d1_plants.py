"""D1: 플랜트별 대표 회로 정상점·수지·민감도·오류 경계 (CDU 회귀 포함). 수치는 synthetic assumption 이다."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from common.config_models import load_plant, load_yaml
from simulator.physics.network import OperatingPoint
from simulator.physics_adapter import CoupledCase, PhysicsBundle, load_physics

ROOT = Path(__file__).resolve().parents[1]
PLANTS = ["cdu", "hou", "rfcc", "rhds", "vrds"]


def case(key: str) -> CoupledCase:
    return CoupledCase.model_validate(load_yaml(ROOT / f"config/physics_{key}_integrated.yaml"))


@pytest.mark.parametrize("key", PLANTS)
def test_cli_normal_point_and_balances(key):
    run = subprocess.run([sys.executable, "-m", "simulator.physics", "--config", f"config/physics_{key}_integrated.yaml"],
                         cwd=ROOT, capture_output=True, text=True, check=True)
    out = json.loads(run.stdout)
    r = out["result"]
    for u in r["units"]:
        assert abs(u["pressure_residual_pa"]) < 1e-4
    assert out["plant_id"] == f"PL-{key.upper()}" and out["provenance"] == "synthetic assumption"
    assert abs(r["mass_residual_kg_s"]) < 1e-9 and abs(r["pump_pressure_residual_pa"]) < 1e-4
    assert all(abs(b["pressure_residual_pa"]) < 1e-4 for b in r["branches"])
    assert sum(b["flow_m3_s"] for b in r["branches"]) == pytest.approx(r["total_flow_m3_s"], rel=1e-9)
    plant = load_plant(ROOT / f"config/plant_{key}.yaml")
    flow_tag = next(t for t in plant.equipment_by_id()[out["equip_id"]].tags if t.signal == "flow")
    assert r["pump_flow_m3_s"] * 3600 == pytest.approx(plant.control_by_key()[(out["equip_id"], flow_tag.tag_id)][0], abs=0.01)


@pytest.mark.parametrize("key", PLANTS)
def test_resistance_speed_and_flow_split_sensitivity(key):
    c = case(key)
    def solve(network=c.network, point=c.default_point):
        return c.model_copy(update={"network": network}).supply().solve(point)

    base = solve()
    product = c.downstream_branch
    # 제품 분기 배관 저항 +50 %: 제품 유량 ↓, 헤더 압력 ↑, 다른 분기 유량 ↑
    branches = [b.model_copy(update={"pipes": {k: p.model_copy(update={"minor_loss_k": p.minor_loss_k * 1.5 + 1})
                                               for k, p in b.pipes.items()}}) if b.branch_id == product else b
                for b in c.network.branches]
    stiff = solve(c.network.model_copy(update={"branches": branches}))
    flow = lambda res, bid: next(b.flow_m3_s for b in res.branches if b.branch_id == bid)
    other = next(b.branch_id for b in c.network.branches if b.branch_id != product)
    assert flow(stiff, product) < flow(base, product) and flow(stiff, other) > flow(base, other)
    assert stiff.header_abs_pa > base.header_abs_pa
    # 회전수: 유량·헤더 압력 단조 증가
    speeds = [solve(point=c.default_point.model_copy(update={"speed_rpm": c.default_point.speed_rpm * r}))
              for r in (0.9, 1.0, 1.05)]
    assert [s.flow_m3_s for s in speeds] == sorted(s.flow_m3_s for s in speeds)
    assert [s.header_abs_pa for s in speeds] == sorted(s.header_abs_pa for s in speeds)
    for res in (stiff, *speeds):
        assert abs(res.mass_residual_kg_s) < 1e-9


@pytest.mark.parametrize("key", PLANTS)
def test_invalid_parameters_and_unsolvable_boundaries_are_rejected(key):
    c = case(key)
    data = load_yaml(ROOT / f"config/physics_{key}_integrated.yaml")
    with pytest.raises(ValidationError):
        CoupledCase.model_validate({**data, "network": {**data["network"], "density_kg_m3": -1}})
    pump = data["network"]["pump"]
    with pytest.raises(ValidationError):
        CoupledCase.model_validate({**data, "network": {**data["network"], "pump": {**pump, "rated_head_m": pump["shutoff_head_m"]}}})
    with pytest.raises(ValidationError):
        OperatingPoint(suction_abs_pa=c.default_point.suction_abs_pa, speed_rpm=1, valve_openings={c.control_valve: 1.5})
    with pytest.raises(ValueError, match="등록되지 않은 밸브"):
        c.network.solve(c.default_point.model_copy(update={"valve_openings": {"virtual:missing": 0.5}}))
    # 다른 분기 출구압을 체절 헤더압보다 높이면 그 분기는 역류가 필요하다 → 거절
    shutoff = c.default_point.suction_abs_pa + c.network.density_kg_m3 * 9.80665 * c.network.pump.shutoff_head_m
    other = next(b for b in c.network.branches if b.branch_id != c.downstream_branch)
    blocked = c.network.model_copy(update={"branches": [b.model_copy(update={"outlet_abs_pa": shutoff * 1.2}) if b is other else b
                                                         for b in c.network.branches]})
    with pytest.raises(ValueError, match="양의 순방향 유량|양정이 양수인"):
        blocked.solve(c.default_point)
    # 흡입 저항이 과도하면 펌프 흡입 절대압이 음수 → 거절
    with pytest.raises(ValueError, match="흡입|운전점이 없|양의 순방향"):
        c.network.solve(c.default_point, suction_resistance=1e15)
    with pytest.raises(ValueError):
        c.network.solve(c.default_point, suction_resistance=-1)


def test_cli_rejects_bad_point_with_exit_code_2(tmp_path):
    point = tmp_path / "point.json"
    point.write_text(json.dumps({"suction_abs_pa": -1, "speed_rpm": 3600}), encoding="utf-8")
    run = subprocess.run([sys.executable, "-m", "simulator.physics", "--config", "config/physics_hou_integrated.yaml",
                          "--point", str(point)], cwd=ROOT, capture_output=True, text=True)
    assert run.returncode == 2 and run.stderr


@pytest.mark.parametrize("key", PLANTS)
def test_every_equipment_is_in_a_circuit_or_excluded_with_reason(key):
    plant = load_plant(ROOT / f"config/plant_{key}.yaml")
    bundle = load_physics(plant, ROOT / f"config/physics_{key}_twin.yaml")
    assert isinstance(bundle, PhysicsBundle)
    assert {e.equip_id for e in plant.equipment} == set(bundle.roles) | set(bundle.exclusions)
    assert not set(bundle.roles) & set(bundle.exclusions)
    assert all(len(reason) > 20 for reason in bundle.exclusions.values())
    # 회로당 복수 설비(결합 회로) 또는 단일 회로 여러 개
    assert sum(len(c["equipment"]) for c in bundle.circuits()) >= 2


def test_ontology_and_3d_mapping_have_no_mismatch():
    diag = ROOT.parent / "sk-clx-diag-agent"
    if not (diag / "ontology/source.sql").exists():
        pytest.skip("진단 저장소 체크아웃이 같은 상위 폴더에 없음")
    from scripts.twin_ontology_mapping import build
    _, problems = build(diag)
    # PU-D5a 의 새 펌프 다섯 대는 PU-D5b 에서 온톨로지·3D 에 들어간다. 그 밖의 불일치는 없어야 한다.
    new = ("P-8101B", "P-1101B", "P-2101-S2", "P-3101-S2", "P-4101-S2")
    assert [p for p in problems if not any(n in p for n in new)] == []
