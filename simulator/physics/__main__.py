"""CDU 정상 유압 운전점을 JSON으로 계산한다."""
import argparse
import json
from pathlib import Path
from typing import Literal

import yaml

from common.config_models import StrictModel, load_yaml
from simulator.physics.network import Network, NetworkResult, OperatingPoint, VirtualID


class NetworkCase(StrictModel):
    plant_id: str
    unit_id: str
    equip_id: str
    suction_node: VirtualID
    discharge_node: VirtualID
    provenance: Literal["synthetic assumption"]
    network: Network
    default_point: OperatingPoint


class CaseResult(StrictModel):
    plant_id: str
    unit_id: str
    equip_id: str
    suction_node: str
    discharge_node: str
    provenance: str
    operating_point: OperatingPoint
    result: NetworkResult


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/physics_cdu_network.yaml"))
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--point", type=Path, help="운전점 입력 JSON 파일")
    group.add_argument("--schema", choices=("config", "point", "result"), help="지정한 JSON Schema 출력")
    args = parser.parse_args()
    if args.schema:
        model = {"config": NetworkCase, "point": OperatingPoint, "result": CaseResult}[args.schema]
        print(json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2))
        return
    try:
        case = NetworkCase.model_validate(load_yaml(args.config))
        point = OperatingPoint.model_validate_json(args.point.read_bytes()) if args.point else case.default_point
        output = CaseResult(plant_id=case.plant_id, unit_id=case.unit_id, equip_id=case.equip_id,
                            suction_node=case.suction_node, discharge_node=case.discharge_node,
                            provenance=case.provenance, operating_point=point, result=case.network.solve(point))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        parser.error(str(exc))
    print(output.model_dump_json(indent=2, exclude_none=True))


if __name__ == "__main__":
    main()
