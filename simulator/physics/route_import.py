"""합성 3D 배관 경로 한 건을 기존 Pipe 입력으로 변환한다 (D3: 명시 매핑이 있을 때만).

시각용 합성 경로 길이로 물리 설정을 자동으로 덮어쓰지 않는다. 경로 간선이 물리 회로의 **같은 배관**이라는
명시 매핑(`--mapping` YAML 항목: plant·edge_index·from·to·physics_config·physical_pipe)이 있고, 출발·도착 설비와
물리 배관 ID 가 실제로 일치할 때만 Pipe 입력을 출력한다. 마찰계수·국부손실 K 는 호출자가 근거와 함께 지정한다.
출력은 표준 출력뿐이며 설정 파일을 수정하지 않는다.
"""
import argparse
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from simulator.physics.pipe import Pipe
from simulator.physics.pump import Positive


class RoutedPipe(BaseModel):
    edge_index: int = Field(ge=0)
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    length_m: Positive
    inner_diameter_m: Positive
    outer_diameter_m: Positive

    @model_validator(mode="after")
    def validate_diameters(self) -> "RoutedPipe":
        if self.inner_diameter_m >= self.outer_diameter_m:
            raise ValueError("내경은 외경보다 작아야 합니다.")
        return self


class RoutingDocument(BaseModel):
    schema_version: Literal[1]
    plant: str
    provenance: Literal["synthetic"]
    units: dict[str, Literal["m"]]
    pipes: list[RoutedPipe]

    @model_validator(mode="after")
    def validate_layout(self) -> "RoutingDocument":
        if set(self.units) != {"coordinates", "length", "diameter"}:
            raise ValueError("경로 좌표·길이·직경의 m 단위를 모두 선언해야 합니다.")
        if len({row.edge_index for row in self.pipes}) != len(self.pipes):
            raise ValueError("배관 간선 인덱스가 중복됐습니다.")
        return self


class PipeMapping(BaseModel):
    """경로 간선 ↔ 물리 배관 1:1 대응. 같은 물리 배관이라는 근거(note)를 적는다."""
    plant: str
    edge_index: int = Field(ge=0)
    from_: str = Field(alias="from")
    to: str
    physics_config: Path
    physical_pipe: str
    note: str = Field(min_length=1)


class MappingFile(BaseModel):
    mappings: list[PipeMapping] = []


def physical_pipe_ids(config: Path) -> set[str]:
    from common.config_models import load_yaml
    data = load_yaml(config)
    network = data.get("network", {})
    ids = {key for branch in network.get("branches", []) for key in branch.get("pipes", {})}
    ids |= {f"{branch['branch_id']}/booster-discharge" for branch in network.get("branches", []) if branch.get("booster")}
    if "suction_pipe" in data:                                   # 단일 회로
        ids |= {"suction_pipe", "discharge_pipe"}
    return ids


def read_pipe(path: Path, edge_index: int, darcy_factor: float, minor_loss_k: float,
              mapping: Path | None = None) -> Pipe:
    document = RoutingDocument.model_validate_json(path.read_bytes())
    route = next((row for row in document.pipes if row.edge_index == edge_index), None)
    if route is None:
        raise ValueError(f"{document.plant}: 연결된 배관 간선 {edge_index}가 없습니다.")
    if mapping is None:
        raise ValueError("물리 배관 명시 매핑(--mapping)이 없습니다. 시각 경로 길이를 물리 설정에 자동 적용하지 않습니다.")
    from common.config_models import load_yaml
    entries = MappingFile.model_validate(load_yaml(mapping)).mappings
    entry = next((e for e in entries if e.plant == document.plant and e.edge_index == edge_index), None)
    if entry is None:
        raise ValueError(f"{document.plant} 간선 {edge_index}: 같은 물리 배관이라는 매핑 항목이 없습니다.")
    if (route.from_, route.to) != (entry.from_, entry.to):
        raise ValueError(f"{document.plant} 간선 {edge_index}: 경로 {route.from_}→{route.to} 가 매핑 {entry.from_}→{entry.to} 와 다릅니다.")
    config = entry.physics_config if entry.physics_config.is_absolute() else mapping.parent / entry.physics_config
    if entry.physical_pipe not in physical_pipe_ids(config):
        raise ValueError(f"{config.name}: 물리 배관 {entry.physical_pipe} 가 없습니다.")
    return Pipe(length_m=route.length_m, diameter_m=route.inner_diameter_m,
                darcy_factor=darcy_factor, minor_loss_k=minor_loss_k)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--edge", type=int, required=True)
    parser.add_argument("--darcy-factor", type=float, required=True)
    parser.add_argument("--minor-loss-k", type=float, required=True)
    parser.add_argument("--mapping", type=Path, help="경로 간선 ↔ 물리 배관 1:1 매핑 YAML (없으면 변환하지 않는다)")
    args = parser.parse_args()
    try:
        pipe = read_pipe(args.path, args.edge, args.darcy_factor, args.minor_loss_k, args.mapping)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(pipe.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
