import json

import pytest

from simulator.physics.route_import import read_pipe


def test_routed_inner_diameter_is_used_and_unresolved_edge_is_rejected(tmp_path):
    path = tmp_path / "routes.json"
    data = {
        "schema_version": 1, "plant": "CDU", "provenance": "synthetic",
        "units": {"coordinates": "m", "length": "m", "diameter": "m"},
        "pipes": [{"edge_index": 5, "length_m": 13.375,
                   "inner_diameter_m": 0.32, "outer_diameter_m": 0.34, "elbow_count": 4}],
        "unresolved": [{"edge_index": 6}],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    pipe = read_pipe(path, 5, 0.025, 4)
    assert (pipe.length_m, pipe.diameter_m, pipe.minor_loss_k) == (13.375, 0.32, 4)
    assert pipe.pressure_loss_pa(0.1, 850) > 0
    with pytest.raises(ValueError, match="간선 6"):
        read_pipe(path, 6, 0.025, 4)
    data["units"]["diameter"] = "mm"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="diameter"):
        read_pipe(path, 5, 0.025, 4)
