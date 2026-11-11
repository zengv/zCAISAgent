import time
from typing import Any

from pydantic import BaseModel, Field


class RunEvent(BaseModel):
    type: str
    run_id: str
    step: int = 0
    node: str | None = None
    # 可选：所属 span 名与父 span id（OTel 映射用）；默认 None 向后兼容。
    span: str | None = None
    parent_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    ts: float = Field(default_factory=time.time)
