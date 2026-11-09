"""태그별 주기와 자릿수로 샘플을 만든다 (설계 §4.4)."""
import time
from datetime import datetime, timedelta

from common.config_models import load_plant, load_runtime
from common.sim_clock import KST
from simulator.generator import SampleGenerator, make_signal_model
from simulator.rfcc_model import RfccSignalModel
from simulator.signal_model import SignalModel

T0 = datetime(2026, 11, 9, 10, 0, tzinfo=KST)


def test_model_factory_follows_plant_signal_model(plant_path, rfcc_plant_path):
    assert type(make_signal_model(load_plant(plant_path), 1, 10)) is SignalModel
    assert isinstance(make_signal_model(load_plant(rfcc_plant_path), 1, 1), RfccSignalModel)


def test_one_second_window_has_only_one_second_tags_except_on_ten_second_marks(rfcc_plant_path, rfcc_runtime_path):
    gen = SampleGenerator(load_plant(rfcc_plant_path), load_runtime(rfcc_runtime_path))
    at_mark = gen.generate(T0, T0 + timedelta(seconds=1))            # T0 는 10초 격자
    off_mark = gen.generate(T0 + timedelta(seconds=3), T0 + timedelta(seconds=4))
    assert len(at_mark) == 81 and len(off_mark) == 65
    assert all(s.ts.second % 10 == 0 for s in at_mark if s.tag_id.startswith("TI-"))


def test_hour_window_counts_and_speed(rfcc_plant_path, rfcc_runtime_path):
    gen = SampleGenerator(load_plant(rfcc_plant_path), load_runtime(rfcc_runtime_path))
    t = time.monotonic()
    samples = gen.generate(T0, T0 + timedelta(hours=1))
    assert time.monotonic() - t < 10.0
    assert len(samples) == 65 * 3600 + 16 * 360


def test_decimals_follow_tag_config(rfcc_plant_path, rfcc_runtime_path):
    gen = SampleGenerator(load_plant(rfcc_plant_path), load_runtime(rfcc_runtime_path))
    by_tag = {s.tag_id: s for s in gen.generate(T0, T0 + timedelta(seconds=1))}
    assert by_tag["ZI-E-1202"].value == round(by_tag["ZI-E-1202"].value, 5)
    assert len(str(by_tag["ZI-E-1202"].value).split(".")[1]) >= 4        # 3자리로 뭉개지지 않았다
    assert by_tag["VI-P-1101A"].value == round(by_tag["VI-P-1101A"].value, 4)
