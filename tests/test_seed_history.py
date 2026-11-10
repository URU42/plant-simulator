"""시나리오 파일 → 정비·고장 이력 적재 (기획서 7.1, 5.6)."""
from fastapi.testclient import TestClient

from collector.app import create_single_app


def test_dev_scenario_history_is_loaded_at_startup(plant_path, runtime_path, scenario_path):
    with TestClient(create_single_app(plant_path, runtime_path, ":memory:", scenario_path=scenario_path)) as c:
        st = c.app.state.plants["plant"].storage
        assert st.count("maintenance_history") == 3
        wo = st.query_dicts("SELECT * FROM maintenance_history WHERE work_order_id = 'WO-DEV-001'")[0]
        assert wo["equip_id"] == "C-1101" and wo["end_ts"] == "2026-11-02T01:00:00+00:00"
        fl = {r["failure_id"]: r for r in st.query_dicts("SELECT * FROM failure_history")}
        assert fl["FL-DEV-001"]["work_order_id"] == "WO-DEV-001" and fl["FL-DEV-001"]["doc_id"] == "DOC-DEV-BRG-WEAR"
        assert fl["FL-DEV-001"]["end_ts"] == "2026-11-02T01:00:00+00:00"     # 정비 후 복귀 시각
        assert fl["FL-DEV-005"]["equip_id"] == "P-4101"                       # tag_id 만 적어도 설비를 채운다


def test_unknown_scenario_id_stops_collector_startup_by_name(plant_path, runtime_path, tmp_path):
    import pytest

    from common.config_models import ConfigError

    bad = tmp_path / "scenario_bad.yaml"
    bad.write_text("events:\n  - { event_id: E, tag_id: VI-NOPE, pattern: step, start: 2026-10-01T00:00:00+09:00, duration: 1h, failure_id: F }\n")
    with pytest.raises(ConfigError, match="VI-NOPE"):
        with TestClient(create_single_app(plant_path, runtime_path, ":memory:", scenario_path=bad)):
            pass
