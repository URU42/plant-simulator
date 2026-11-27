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


def test_rfcc_limits_are_engineering_not_statistical(config_dir):
    """2026-11-23: RFCC 한계값은 통계 μ±3σ 가 아니라 공학 기준값이다(다른 공장처럼 정상에서 멀다). μ·σ(Nelson 기준)는 그대로."""
    names = ("trip_lo", "alarm_lo", "warn_lo", "warn_hi", "alarm_hi", "trip_hi")
    for t in load_plant(config_dir / "plant_rfcc.yaml").all_tags():
        c, lim = t.control, {n: getattr(t.limits, n) for n in names if getattr(t.limits, n) is not None}
        assert lim and all(abs(v - c.mean) > 10 * c.sigma for v in lim.values()), t.tag_id
        assert list(lim.values()) == sorted(lim.values()), t.tag_id                 # 정지 < 알람 < 경고 < μ < 경고 < 알람 < 정지


@pytest.mark.skipif(not BASELINES.exists(), reason="data-v3 자료 없음")
def test_control_matches_baselines(config_dir):
    base = {(b["plant"], b["equipment"], b["tag"]): b for b in json.loads(BASELINES.read_text())}
    for key in PLANTS:
        plant = load_plant(config_dir / f"plant_{key}.yaml")
        for e in plant.equipment:
            if e.type in ("MT", "GB"):      # 합성 추가 설비는 baselines.json 에 없다
                continue
            for t in e.tags:
                if (key.upper(), e.equip_id, t.tag_id) not in base:   # 자료 밖 합성 추가 태그(단간 압력·추가 계측)도 없다
                    assert t.description, t.tag_id
                    continue
                b = base[(key.upper(), e.equip_id, t.tag_id)]
                assert t.control.mean == b["mean"]
                if t.derive is None:            # 계산 태그는 식이 정하는 변동을 쓰므로 σ 가 자료와 다를 수 있다 (2026-11-19)
                    assert t.control.sigma == b["sigma"]


