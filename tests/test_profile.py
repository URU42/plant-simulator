"""실행 용도(demo·eval)와 정상 운전 변화 대상 확대 (2026-11-23)."""
import pytest

from collector.plant_context import PlantContext
from collector.plants import PlantEntry
from common.config_models import ConfigError, apply_profile, load_plant, load_runtime
from simulator.fault import load_fault_modes, plan


def test_apply_profile(runtime_path):
    rt = load_runtime(runtime_path)
    assert apply_profile(rt, "demo") is rt
    ev = apply_profile(rt, "eval")
    assert ev.quiet_normal is False and ev.instrument_faults.enabled is True
    assert ev.instrument_faults.spike == rt.instrument_faults.spike and ev.seed == rt.seed    # 빈도·크기·시드는 파일 값 그대로
    assert rt.quiet_normal is True and rt.instrument_faults.enabled is False                 # 원본은 그대로
    with pytest.raises(ConfigError):
        apply_profile(rt, "training")


def test_db_is_pinned_to_one_profile(plant_path, runtime_path, tmp_path):
    """정답지의 계측 이상은 지금 설정으로 과거까지 다시 계산하므로, 다른 용도로 만든 DB 는 열지 않는다."""
    entry = PlantEntry(key="rhds", plant=str(plant_path), runtime=str(runtime_path), db=str(tmp_path / "a.db"))
    ctx = PlantContext.open(entry, modes={}, profile="eval")
    assert ctx.runtime.quiet_normal is False and ctx.book.instrument is not None   # 생성기와 정답지가 같은 설정을 본다
    ctx.close()
    PlantContext.open(entry, modes={}, profile="eval").close()                      # 같은 용도는 이어 쓴다
    with pytest.raises(ConfigError, match="profile 'eval'"):
        PlantContext.open(entry, modes={}, profile="demo")


def test_db_with_data_before_profiles_counts_as_demo(plant_path, runtime_path, tmp_path):
    entry = PlantEntry(key="rhds", plant=str(plant_path), runtime=str(runtime_path), db=str(tmp_path / "b.db"))
    ctx = PlantContext.open(entry, modes={})
    ctx.storage.execute("DELETE FROM run_meta")                                     # 이 기능 전에 만든 DB 흉내
    ctx.storage.store_batch("b", "T", "2026-11-12T00:00:00.000000Z", [("C-3101", "FI-C3101", "2026-11-12T00:00:00.000000Z", 1.0)])
    ctx.close()
    with pytest.raises(ConfigError):
        PlantContext.open(entry, modes={}, profile="eval")
    PlantContext.open(entry, modes={}, profile="demo").close()


@pytest.mark.parametrize("plant_key, equip, mode, own", [
    ("hou", "C-2201", "OP-LOAD-CHANGE", {"FI-2201", "PI-2201B", "SI-2201"}),
    ("hou", "C-2301", "OP-LOAD-CHANGE", {"FI-2301", "PI-2301B", "SI-2301"}),
    ("cdu", "P-8101A", "OP-THROUGHPUT-CHANGE", {"FI-8101A", "PI-8101B", "II-8101A"}),
    ("rfcc", "P-1101A", "OP-THROUGHPUT-CHANGE", {"FI-P-1101A", "PD-P-1101A", "II-P-1101A"}),
    ("hou", "P-2101", "OP-THROUGHPUT-CHANGE", {"FI-2101", "PI-2101B", "II-2101"}),
    ("rhds", "P-3101", "OP-THROUGHPUT-CHANGE", {"FI-P3101", "PI-D-P3101", "II-P3101"}),
    ("vrds", "P-4101", "OP-THROUGHPUT-CHANGE", {"FI-P4101", "PI-D-P4101", "II-P4101"}),
])
def test_normal_operation_change_targets(config_dir, plant_key, equip, mode, own):
    """HOU 압축기와 다섯 공장 원료 공급 펌프에도 정상 운전 변화(코드 1)를 주입할 수 있고, 자기 유량이 움직이며 하류로 전파된다."""
    modes = load_fault_modes(config_dir / "fault_modes.yaml")
    plant = load_plant(config_dir / f"plant_{plant_key}.yaml")
    tags = plan(plant, modes[mode], equip, plant.control_by_key())
    assert modes[mode].label.code == 1
    assert own <= {k[1] for k in tags if k[0] == equip}
    assert any(tp.via == "process" for tp in tags.values())


def test_existing_load_change_targets_unchanged(config_dir):
    """자기 유량·토출 압력 효과를 더해도 터빈·전동기에는 그 태그가 없어 계획이 그대로다."""
    modes = load_fault_modes(config_dir / "fault_modes.yaml")
    plant = load_plant(config_dir / "plant_rhds.yaml")
    tags = plan(plant, modes["OP-LOAD-CHANGE"], "M-3101", plant.control_by_key())
    assert {k for k in tags if k[0] == "M-3101"} == {("M-3101", "II-M3101"), ("M-3101", "SI-M3101")}
