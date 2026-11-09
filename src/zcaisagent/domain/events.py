import time
from typing import Any

from pydantic import BaseModel, Field


class RunEvent(BaseModel):
    type: str
    run_id: str
    step: int = 0
    node: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    ts: float = Field(default_factory=time.time)
