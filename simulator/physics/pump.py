"""고정 효율의 원심 펌프 Q–H 곡선과 회전수 상사법칙."""
from math import isfinite, sqrt
from typing import Annotated

from pydantic import Field, model_validator

from common.config_models import StrictModel

Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Nonnegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
GRAVITY_M_S2 = 9.80665


def require_positive(value: float, quantity: str) -> None:
    if not isfinite(value) or value <= 0:
        raise ValueError(f"{quantity}: 유한한 양수가 필요합니다.")


def require_flow(flow_m3_s: float) -> None:
    if not isfinite(flow_m3_s) or flow_m3_s < 0:
        raise ValueError("유량은 유한한 음이 아닌 값이어야 합니다.")


class Pump(StrictModel):
    reference_speed_rpm: Positive
    rated_flow_m3_s: Positive
    shutoff_head_m: Positive
    rated_head_m: Positive
    efficiency: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def check_curve(self) -> "Pump":
        if self.rated_head_m >= self.shutoff_head_m:
            raise ValueError("정격 양정은 체절 양정보다 작아야 합니다.")
        return self

    @property
    def curvature(self) -> float:
        return (self.shutoff_head_m - self.rated_head_m) / self.rated_flow_m3_s**2

    def shutoff_at(self, speed_rpm: float) -> float:
        require_positive(speed_rpm, "회전수")
        return self.shutoff_head_m * (speed_rpm / self.reference_speed_rpm)**2

    def head_m(self, flow_m3_s: float, speed_rpm: float) -> float:
        require_flow(flow_m3_s)
        head = self.shutoff_at(speed_rpm) - self.curvature * flow_m3_s**2
        if head < 0:
            raise ValueError("요청한 유량은 펌프의 양정 곡선 범위를 벗어났습니다.")
        return head

    def flow_at_head(self, head_m: float, speed_rpm: float) -> float:
        if not isfinite(head_m) or head_m < 0:
            raise ValueError("펌프 양정은 유한한 음이 아닌 값이어야 합니다.")
        return sqrt(max(0.0, self.shutoff_at(speed_rpm) - head_m) / self.curvature)
