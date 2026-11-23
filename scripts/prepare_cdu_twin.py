"""충돌 없는 로컬 CDU 데모 설정과 비밀값을 .cache/d4에만 만든다."""
import secrets
from pathlib import Path
from tempfile import mkdtemp

import yaml

from common.config_models import load_yaml

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache/d4"


def main():
    CACHE.mkdir(mode=0o700, parents=True, exist_ok=True)
    run = Path(mkdtemp(prefix="run.", dir=CACHE))
    values = {
        "INGEST_TOKEN": secrets.token_urlsafe(32),
        "AGENT_INTERNAL_TOKEN": secrets.token_urlsafe(32),
        "AUTH_JWT_SECRET": secrets.token_urlsafe(40),
        "AUTH_ADMIN_USERNAME": "cdu-d4-demo",
        "AUTH_ADMIN_PASSWORD": secrets.token_urlsafe(20),
    }
    env = run / "secrets.env"
    env.touch(mode=0o600, exist_ok=False)
    env.write_text("".join(f"{name}={value}\n" for name, value in values.items()), encoding="utf-8")
    # 출처의 로컬 상대 경로를 복사본에서 잃지 않도록 절대 경로로 저장한다.
    runtime = run / "runtime.yaml"
    config = load_yaml(ROOT / "config/runtime_cdu.yaml")
    config["physics"] = str(ROOT / "config/physics_cdu_integrated.yaml")
    runtime.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    (run / "plants.yaml").write_text(yaml.safe_dump({
        "fault_modes": str(ROOT / "config/fault_modes.yaml"),
        "plants": [{"key": "cdu", "plant": str(ROOT / "config/plant_cdu.yaml"),
                    "runtime": str(runtime), "db": str(run / "cdu.db")}],
    }), encoding="utf-8")
    print(run)


if __name__ == "__main__":
    main()
