"""정상 데이터: 재현성, 형식, 한계 이탈 없음."""
import csv

from scripts.normal_data import write_plant


def test_normal_data_is_reproducible_and_within_limits(tmp_path):
    results = []
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        results.append(write_plant("hou", tmp_path / name, 0.1, 0.05, "", True))
    assert results[0] == results[1] and not results[0]["limit_hits"]
    rows = list(csv.reader((tmp_path / "a/train/hou_P-2101-S2.csv").open(encoding="utf-8")))
    assert rows[0][0] == "timestamp" and rows[0][-3:] == ["label", "scenario_id", "event_id"]
    assert len(rows) == 1 + 360 and all(len(r) == len(rows[0]) and r[-3] == "0" for r in rows[1:])
    assert rows[1][0].endswith("00:00:00+09:00")
