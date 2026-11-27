import json

import pytest

from simulator.physics.route_import import read_pipe


def _routes(tmp_path, **unit):
    path = tmp_path / "routes.json"
    data = {
        "schema_version": 1, "plant": "CDU", "provenance": "synthetic",
        "units": {"coordinates": "m", "length": "m", "diameter": "m", **unit},
        "pipes": [{"edge_index": 5, "from": "P-8303A", "to": "AT", "length_m": 13.375,
                   "inner_diameter_m": 0.32, "outer_diameter_m": 0.34, "elbow_count": 4}],
        "unresolved": [{"edge_index": 6}],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _mapping(tmp_path, config_dir, **override):
    entry = {"plant": "CDU", "edge_index": 5, "from": "P-8303A", "to": "AT",
             "physics_config": str(config_dir / "physics_cdu_p8303a.yaml"), "physical_pipe": "discharge_pipe",
             "note": "테스트용 합성 매핑", **override}
    path = tmp_path / "mapping.yaml"
    path.write_text(json.dumps({"mappings": [entry]}), encoding="utf-8")
    return path


def test_route_is_applied_only_with_explicit_same_pipe_mapping(tmp_path, config_dir):
    routes = _routes(tmp_path)
    with pytest.raises(ValueError, match="자동 적용하지 않습니다"):
        read_pipe(routes, 5, 0.025, 4)                       # 매핑 없이 시각 길이를 쓰지 않는다
    pipe = read_pipe(routes, 5, 0.025, 4, _mapping(tmp_path, config_dir))
    assert (pipe.length_m, pipe.diameter_m, pipe.minor_loss_k) == (13.375, 0.32, 4)
    assert pipe.pressure_loss_pa(0.1, 850) > 0
    with pytest.raises(ValueError, match="간선 6"):
        read_pipe(routes, 6, 0.025, 4, _mapping(tmp_path, config_dir))
    with pytest.raises(ValueError, match="와 다릅니다"):
        read_pipe(routes, 5, 0.025, 4, _mapping(tmp_path, config_dir, to="DR"))
    with pytest.raises(ValueError, match="물리 배관"):
        read_pipe(routes, 5, 0.025, 4, _mapping(tmp_path, config_dir, physical_pipe="virtual:none"))
    with pytest.raises(ValueError, match="diameter"):
        read_pipe(_routes(tmp_path, diameter="mm"), 5, 0.025, 4, _mapping(tmp_path, config_dir))
