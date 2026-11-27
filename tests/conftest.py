"""공용 픽스처: 저장소의 실제 설정 파일 경로. 기본 공장(plant_path·runtime_path)은 RHDS."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

# CI(Jenkinsfile: -m 'not slow')에서 도는 테스트: 구성 검사·k8s 매니페스트·메시지 형식·시드 동일성·수집기 API.
# 나머지는 모두 slow 표시가 붙는다. 로컬 기본 실행(uv run pytest)은 전체를 돈다.
CI_TESTS = {
    "test_config.py", "test_control_config.py", "test_multi_plant_configs.py", "test_rfcc_config.py",   # 구성 검사
    "test_k8s_manifests.py",                                                                            # k8s 매니페스트
    "test_message.py",                                                                                  # 메시지 형식
    "test_instrument.py::test_schedule_is_deterministic_and_off_by_default",                            # 시드 동일성
    "test_instrument.py::test_schedule_same_as_wook_pl_prefix_schedule",
    "test_collector_core.py", "test_collector_ingest.py", "test_collector_multi.py",                    # 수집기 API
    "test_collector_queries.py", "test_collector_rfcc.py", "test_collector_settings.py",
}


def pytest_collection_modifyitems(items):
    for item in items:
        if not {item.path.name, f"{item.path.name}::{item.originalname}"} & CI_TESTS:
            item.add_marker(pytest.mark.slow)


@pytest.fixture(scope="session")
def plant_path() -> Path:
    return CONFIG_DIR / "plant_rhds.yaml"


@pytest.fixture(scope="session")
def runtime_path() -> Path:
    return CONFIG_DIR / "runtime_rhds.yaml"


# --- RFCC 구성안 (설계 docs/superpowers/specs/2026-11-09-rfcc-simulator-design.md) ---

@pytest.fixture(scope="session")
def rfcc_plant_path() -> Path:
    return CONFIG_DIR / "plant_rfcc.yaml"


@pytest.fixture(scope="session")
def rfcc_runtime_path() -> Path:
    return CONFIG_DIR / "runtime_rfcc.yaml"


@pytest.fixture(scope="session")
def config_dir() -> Path:
    return CONFIG_DIR
