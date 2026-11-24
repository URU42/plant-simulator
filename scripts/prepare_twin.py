"""충돌 없는 로컬 디지털 트윈 시연 설정과 비밀값을 .cache/d4 에만 만든다.

기본은 다섯 공장(CDU·HOU·RFCC·RHDS·VRDS) 모두 물리 결합을 켠 수집기 하나다. `--plants cdu` 처럼 좁힐 수 있다.
생성물(비밀값·DB·plants.yaml)은 Git 제외 경로에만 두고 커밋하지 않는다.
"""
import argparse
import secrets
from pathlib import Path
from tempfile import mkdtemp

import yaml

from common.config_models import load_yaml

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache/d4"
PLANTS = ("cdu", "hou", "rfcc", "rhds", "vrds")


def prepare(plants: list[str], username: str = "twin-d4-demo") -> Path:
    CACHE.mkdir(mode=0o700, parents=True, exist_ok=True)
    run = Path(mkdtemp(prefix="run.", dir=CACHE))
    values = {
        "INGEST_TOKEN": secrets.token_urlsafe(32),
        "AGENT_INTERNAL_TOKEN": secrets.token_urlsafe(32),
        "AUTH_JWT_SECRET": secrets.token_urlsafe(40),
        "AUTH_ADMIN_USERNAME": username,
        "AUTH_ADMIN_PASSWORD": secrets.token_urlsafe(20),
    }
    env = run / "secrets.env"
    env.touch(mode=0o600, exist_ok=False)
    env.write_text("".join(f"{name}={value}\n" for name, value in values.items()), encoding="utf-8")
    entries = []
    for key in plants:
        # 출처의 로컬 상대 경로를 복사본에서 잃지 않도록 절대 경로로 저장한다.
        runtime = run / f"runtime_{key}.yaml"
        config = load_yaml(ROOT / f"config/runtime_{key}.yaml")
        # 공장별 회로 묶음(설비별 제외 사유 포함). CDU: 결합 회로 + P-8303A + F-8201.
        config["physics"] = str(ROOT / f"config/physics_{key}_twin.yaml")
        runtime.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
        entries.append({"key": key, "plant": str(ROOT / f"config/plant_{key}.yaml"),
                        "runtime": str(runtime), "db": str(run / f"{key}.db")})
    (run / "plants.yaml").write_text(yaml.safe_dump({
        "fault_modes": str(ROOT / "config/fault_modes.yaml"), "plants": entries,
    }), encoding="utf-8")
    return run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plants", default=",".join(PLANTS), help="쉼표 구분 공장 키 (기본: 다섯 공장)")
    args = parser.parse_args()
    plants = [p.strip() for p in args.plants.split(",") if p.strip()]
    unknown = set(plants) - set(PLANTS)
    if not plants or unknown:
        parser.error(f"알 수 없는 공장: {', '.join(sorted(unknown)) or '(없음)'}")
    print(prepare(plants))


if __name__ == "__main__":
    main()
