"""공용 픽스처: 저장소의 실제 설정 파일 경로. 기본 공장(plant_path·runtime_path)은 RHDS."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


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
