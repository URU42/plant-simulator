"""공장 목록 파일 (다중 공장 설계 §4.7). 수집 서버의 유일한 설정 입력.

```yaml
plants:
  - { key: cdu, plant: config/plant_cdu.yaml, runtime: config/runtime_cdu.yaml, db: data/cdu.db }
```
key 는 URL 접두어(`/cdu/...`)이자 로그·루트 목록의 이름이다. 파일 존재는 서버가 공장을 열 때 확인한다.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from common.config_models import ConfigError, load_yaml


class PlantEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[a-z0-9-]+$")   # URL 접두어
    plant: str                                   # 구성안 yaml
    runtime: str                                 # 실행 설정 yaml
    db: str                                      # SQLite 파일


class PlantsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plants: list[PlantEntry]

    @model_validator(mode="after")
    def _unique(self) -> "PlantsConfig":
        keys = [p.key for p in self.plants]
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        if dupes:
            raise ConfigError(f"duplicate plant key: {', '.join(dupes)}")
        dbs = [p.db for p in self.plants]
        if len(set(dbs)) != len(dbs):
            raise ConfigError("two plants share one db file; each plant needs its own")
        return self


def load_plants(path: str | Path) -> PlantsConfig:
    return PlantsConfig.model_validate(load_yaml(path))
