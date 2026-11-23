"""합성 3D 배관 경로 한 건을 기존 Pipe 입력으로 변환한다."""
import argparse
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from simulator.physics.pipe import Pipe
from simulator.physics.pump import Positive


class RoutedPipe(BaseModel):
    edge_index: int = Field(ge=0)
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


def read_pipe(path: Path, edge_index: int, darcy_factor: float, minor_loss_k: float) -> Pipe:
    document = RoutingDocument.model_validate_json(path.read_bytes())
    route = next((row for row in document.pipes if row.edge_index == edge_index), None)
    if route is None:
        raise ValueError(f"{document.plant}: 연결된 배관 간선 {edge_index}가 없습니다.")
    return Pipe(length_m=route.length_m, diameter_m=route.inner_diameter_m,
                darcy_factor=darcy_factor, minor_loss_k=minor_loss_k)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--edge", type=int, required=True)
    parser.add_argument("--darcy-factor", type=float, required=True)
    parser.add_argument("--minor-loss-k", type=float, required=True)
    args = parser.parse_args()
    try:
        pipe = read_pipe(args.path, args.edge, args.darcy_factor, args.minor_loss_k)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(pipe.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
