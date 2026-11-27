"""D2 평가 배치: 관측·정답 분리, 재현성, 결측, 사건 누출, 계측 이상의 공정 불변, 태그별 지연·회복, 측정 주기."""
import csv
import gzip
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from common.config_models import load_plant
from common.message import Sample
from scripts import eval_batch as eb
from simulator.fault import load_fault_modes
from simulator.model import derive_rule

ROOT = Path(__file__).resolve().parents[1]
MODES = load_fault_modes(ROOT / "config/fault_modes.yaml")
BY_NAME = {c.name: c for c in eb.CASES}
SMALL = tuple(BY_NAME[n] for n in ("stable-rhds", "fault-dropout", "i6"))


@pytest.fixture(scope="module")
def pairs():
    cache = {}

    def get(name, seed=7):
        if (name, seed) not in cache:
            cache[name, seed] = eb.build_pair(BY_NAME[name], seed, MODES)
        return cache[name, seed]
    return get


@pytest.fixture(scope="module")
def batch(tmp_path_factory):
    root = tmp_path_factory.mktemp("eval")
    eb.write_batch(root / "obs", root / "truth", {"train": [1], "val": [2], "test": []}, SMALL)
    return root


def _truth(root):
    return json.loads((root / "truth/truth.json").read_text())


def _runs(root):
    return json.loads((root / "obs/manifest.json").read_text())["runs"]


def _t(iso):
    return datetime.fromisoformat(iso).timestamp()


def test_observations_carry_no_answers(batch):
    """관측 폴더에는 v3 필드와 불투명한 실행 ID 만 있고 정답은 따로 닫힌 폴더에 있다."""
    assert not list((batch / "obs").rglob("*truth*"))
    assert (batch / "truth").stat().st_mode & 0o777 == 0o700
    assert (batch / "truth/truth.json").stat().st_mode & 0o777 == 0o600
    text = (batch / "obs/manifest.json").read_text()
    for t in _truth(batch)["runs"]:
        hidden = [t["group_id"], t["case"], t["scenario"]] + [x for e in t["events"] for x in (e["event_id"], e.get("mode"))]
        assert not any(h and h in text for h in hidden)
    assert {frozenset(r) for r in _runs(batch)} == {frozenset(("run_id", "split", "plant_id", "file", "start_at", "end_at", "rows", "sha256"))}
    assert len({_t(r["end_at"]) - _t(r["start_at"]) for r in _runs(batch)}) == 1, "길이로 시나리오를 알 수 없다"
    for r in _runs(batch):
        rows = list(csv.DictReader(gzip.open(batch / "obs" / r["file"], "rt")))
        assert len(rows) == r["rows"] and set(rows[0]) == set(Sample.model_fields)
        for row in rows[:50]:
            Sample.model_validate(row)          # v3 계약 (모르는 칸 금지)


def test_answer_dir_must_be_separate_and_new(tmp_path):
    for out, ev in (("a", "a"), ("a", "a/truth"), ("a/obs", "a")):
        with pytest.raises(ValueError):
            eb.write_batch(tmp_path / out, tmp_path / ev, {"train": [1]}, ())
    assert not (tmp_path / "a").exists()
    (tmp_path / "o").mkdir()
    with pytest.raises(FileExistsError):
        eb.write_batch(tmp_path / "o", tmp_path / "t", {"train": [1]}, ())


def test_same_seed_same_bytes_other_seed_differs(batch, tmp_path):
    eb.write_batch(tmp_path / "obs", tmp_path / "truth", {"train": [1], "val": [2], "test": []}, SMALL)
    files = [p.relative_to(batch) for p in sorted(batch.rglob("*")) if p.is_file()]
    assert len(files) == 2 * 2 * len(SMALL) + 2
    for f in files:
        assert (batch / f).read_bytes() == (tmp_path / f).read_bytes(), f
    a, b = ({r["sha256"] for r in _runs(batch) if r["split"] == s} for s in ("train", "val"))
    assert not a & b


def test_event_group_and_seed_stay_in_one_split(batch):
    runs = _truth(batch)["runs"]
    for field in (lambda r: [r["group_id"]], lambda r: [r["seed"]], lambda r: [e["event_id"] for e in r["events"]]):
        where = {}
        for r in runs:
            for k in field(r):
                where.setdefault(k, set()).add(r["split"])
        assert where and all(len(s) == 1 for s in where.values())
    on, off = (next(r for r in runs if r["case"] == "i6" and r["seed"] == 1 and r["arm"] == a) for a in ("on", "off"))
    assert on["events"][0]["event_id"] == off["events"][0]["event_id"] and on["split"] == off["split"]


def test_seed_in_two_splits_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="leak"):
        eb.write_batch(tmp_path / "o", tmp_path / "t", {"train": [1], "test": [1]}, ())
    assert not (tmp_path / "o").exists()


def test_pair_shares_conditions_and_differs_only_by_treatment(pairs):
    info, arms = pairs("stable-rhds")
    assert arms[0][1] == arms[1][1] and info["changed_tags"] == []              # 영(null) 쌍
    for name in ("surge", "spike"):
        _, ((_, ref, _), (_, treated, answer)) = pairs(name)
        before = _t(answer["events"][0]["injected_at"])
        assert [s for s in ref if s.ts.timestamp() < before] == [s for s in treated if s.ts.timestamp() < before]
        info = pairs(name)[0]
        assert info["changed_tags"] == sorted(f"{t['equip_id']}/{t['tag_id']}" for t in answer["events"][0]["tags"])


@pytest.mark.parametrize("name", ["spike", "stuck-temp", "dropout", "stuck-twin"])
def test_instrument_fault_leaves_process_state_alone(pairs, monkeypatch, name):
    """보낸 값만 바뀐다: 사건 밖 시각·다른 태그(계산 태그 포함)·공정 값과 물리 해가 기준과 같다."""
    case = BY_NAME[name]
    info, ((_, ref, _), (_, treated, answer)) = pairs(name)
    (event,) = answer["events"]
    assert event["code"] == 2 and event["first_response_at"] is None
    assert event["injected_at"] <= event["first_observed_at"] < event["released_at"]      # 고착 값이 처음 몇 표본과 같을 수 있다
    hit = {(t["equip_id"], t["tag_id"]) for t in event["tags"]}
    a, b = _t(event["injected_at"]), _t(event["released_at"])
    inside = lambda s: (s.equip_id, s.tag_id) in hit and a <= s.ts.timestamp() < b
    assert [s for s in ref if not inside(s)] == [s for s in treated if not inside(s)]

    # 공정 값·물리 상태: 생성기 안에서 계측 층을 켠 실행과 끈 실행의 계측 전 값이 같다
    states = []
    for faults in (True, False):
        seen = []
        original = eb.PlantGenerator._measure

        def record(self, s0, s1, faults=faults):
            apply = original(self, s0, s1) if faults else None

            def spy(t, ep, s):
                seen.append((t.key, ep, s.value))
                return apply(t, ep, s) if apply else s
            return spy
        with monkeypatch.context() as m:
            m.setattr(eb.PlantGenerator, "_measure", record)
            eb.build_pair(case, 7, MODES)
        states.append(seen)
    assert states[0] == states[1]


def test_dropout_is_absent_rows_not_filled(pairs):
    _, ((_, ref, _), (_, treated, answer)) = pairs("dropout")
    (event,) = answer["events"]
    a, b = _t(event["injected_at"]), _t(event["released_at"])
    mine = [s for s in treated if s.equip_id == "P-3102" and a <= s.ts.timestamp() < b]
    assert mine == [] and sum(t["missing"] for t in event["tags"]) == len(ref) - len(treated) > 0
    interval = {t.tag_id: t.interval_sec or 1 for t in load_plant(ROOT / "config/plant_rhds.yaml").equipment_by_id()["P-3102"].tags}
    assert {t["tag_id"]: t["missing"] for t in event["tags"]} == {k: 60 // v for k, v in interval.items()}
    assert all(s.value == s.value for s in treated)         # NaN 없음


def test_missing_samples_delay_observation_not_response(pairs):
    """고장이 누락 구간에서 시작하면 첫 반응은 그대로이고 첫 관측만 늦다."""
    event, drop = pairs("fault-dropout")[1][1][2]["events"]
    flow = next(t for t in event["tags"] if t["tag_id"] == "FI-C3101")
    assert flow["response_delay_sec"] == 1
    assert _t(flow["first_observed_at"]) >= _t(drop["released_at"]) > _t(flow["first_response_at"])


@pytest.mark.parametrize("name", ["surge", "gear-damaged", "stuck-twin"])
def test_one_and_ten_second_sampling(pairs, name):
    info, arms = pairs(name)
    plant = load_plant(ROOT / f"config/plant_{BY_NAME[name].plant}.yaml")
    interval = {(e.equip_id, t.tag_id): t.interval_sec or 1 for e in plant.equipment for t in e.tags}
    assert set(interval.values()) == {1, 10}
    got = eb._by_tag(arms[1][1])
    for key, step in interval.items():
        eps = sorted(got[key])
        assert all(e % step == 0 for e in eps) and len(eps) == eb.WINDOW // step, key


def test_fast_flow_slow_temperature(pairs):
    (event,) = pairs("surge")[1][1][2]["events"]
    tag = {t["tag_id"]: t for t in event["tags"]}
    assert tag["FI-C3101"]["recovery_sec"] == tag["PI-D-C3101"]["recovery_sec"] == 60
    assert tag["TI-D-C3101"]["recovery_sec"] == 300
    assert tag["FI-C3101"]["response_delay_sec"] < tag["PI-D-C3101"]["response_delay_sec"] < tag["FI-P3102"]["response_delay_sec"]
    assert tag["FI-P3102"]["recovery_sec"] == 120           # 하류 1단계
    assert event["restored_at"] == tag["TI-D-C3101"]["restored_at"]
    assert all(_t(t["first_observed_at"]) >= _t(t["first_response_at"]) for t in event["tags"])


def test_damage_stays_until_repair(pairs):
    (hurt,) = pairs("gear-damaged")[1][1][2]["events"]
    tag = {t["tag_id"]: t for t in hurt["tags"]}
    assert hurt["damage_remains"] and hurt["restored_at"] is None and hurt["released_at"]
    assert tag["VI-I-GB3101"]["restored_at"] is None and tag["PI-LO-GB3101"]["recovery_sec"] == 60
    (fixed,) = pairs("gear-repaired")[1][1][2]["events"]
    tag = {t["tag_id"]: t for t in fixed["tags"]}
    assert not fixed["damage_remains"]
    assert _t(tag["VI-I-GB3101"]["restored_at"]) == _t(fixed["repaired_at"]) + 60
    assert _t(tag["TI-B-GB3101"]["restored_at"]) == _t(fixed["repaired_at"]) + 300 == _t(fixed["restored_at"])
    assert _t(tag["PI-LO-GB3101"]["restored_at"]) == _t(fixed["released_at"]) + 60 < _t(fixed["repaired_at"])


@pytest.mark.parametrize("case", [c for c in eb.CASES if c.mode and not c.off], ids=lambda c: c.name)
def test_event_times_are_ordered(pairs, case):
    event = pairs(case.name)[1][1][2]["events"][0]
    t = {k: _t(event[k]) for k in ("injected_at", "first_response_at", "first_observed_at", "released_at")}
    assert t["injected_at"] < t["first_response_at"] <= t["first_observed_at"] < t["released_at"]
    assert (event["restored_at"] is None) == (case.name == "gear-damaged")
    if event["restored_at"]:
        assert _t(event["restored_at"]) > (_t(event["repaired_at"]) if event["repaired_at"] else t["released_at"])
    assert event["code"] == (1 if case.scenario == "normal_load" else 0)


@pytest.mark.parametrize("case", [c for c in eb.CASES if c.off], ids=lambda c: c.name)
def test_interaction_pair_differs(pairs, case):
    info, arms = pairs(case.name)
    assert [a[0] for a in arms] == ["on", "off"] and info["changed_tags"]
    plant = load_plant(ROOT / f"config/plant_{case.plant}.yaml")
    rule = {f"{e.equip_id}/{t.tag_id}" for e in plant.equipment for t in e.tags if derive_rule(e.type, t) == case.off}
    if rule:            # I1·I2 는 공장의 모든 해당 계산 태그에서 꺼진다
        assert set(info["changed_tags"]) <= rule and any(x.startswith(case.equip) for x in info["changed_tags"])
    else:
        assert all(x.startswith(case.equip) for x in info["changed_tags"])
    assert arms[0][2]["events"][0]["injected_at"] == arms[1][2]["events"][0]["injected_at"]


def test_cli(tmp_path):
    cmd = [sys.executable, "-m", "scripts.eval_batch", "--out", str(tmp_path / "o"), "--eval-dir", str(tmp_path / "t"),
           "--train", "5", "--val", "--test", "--cases", "spike"]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=True)
    assert json.loads(r.stdout)["runs"] == 2 and len(list((tmp_path / "o/train").iterdir())) == 2
