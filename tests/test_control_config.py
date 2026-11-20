"""Nelson 관리 기준(μ·σ), 자릿수, RFCC 3σ 한계, 지속 고장 간격 설정 (구성안 ↔ baselines.json)."""
import json
from pathlib import Path

import pytest

from common.config_models import load_plant

PLANTS = ["rfcc", "cdu", "hou", "rhds", "vrds"]
BASELINES = Path.home() / "Downloads/data-v3/평가전용/nelson-20261112/baselines.json"


@pytest.mark.parametrize("key", PLANTS)
def test_every_tag_has_control_and_fine_decimals(config_dir, key):
    plant = load_plant(config_dir / f"plant_{key}.yaml")
    for t in plant.all_tags():
        assert t.control is not None, t.tag_id
        assert 10 ** -t.decimals <= 0.1 * t.control.sigma + 1e-12, t.tag_id


def test_rfcc_limits_are_three_sigma(config_dir):
    for t in load_plant(config_dir / "plant_rfcc.yaml").all_tags():
        c = t.control
        assert t.limits.warn_lo == pytest.approx(c.mean - 3 * c.sigma, abs=1e-4)
        assert t.limits.warn_hi == pytest.approx(c.mean + 3 * c.sigma, abs=1e-4)


@pytest.mark.skipif(not BASELINES.exists(), reason="data-v3 자료 없음")
def test_control_matches_baselines(config_dir):
    base = {(b["plant"], b["equipment"], b["tag"]): b for b in json.loads(BASELINES.read_text())}
    for key in PLANTS:
        plant = load_plant(config_dir / f"plant_{key}.yaml")
        for e in plant.equipment:
            if e.type in ("MT", "GB"):      # 합성 추가 설비는 baselines.json 에 없다
                continue
            for t in e.tags:
                if "(synthetic)" in (t.description or ""):   # 합성 추가 태그도 없다
                    continue
                b = base[(key.upper(), e.equip_id, t.tag_id)]
                assert t.control.mean == b["mean"]
                if t.derive is None:            # 계산 태그는 식이 정하는 변동을 쓰므로 σ 가 자료와 다를 수 있다 (2026-11-19)
                    assert t.control.sigma == b["sigma"]


