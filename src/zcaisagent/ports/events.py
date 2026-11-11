import time
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field


class EventEnvelope(BaseModel):
    """对外投递的事件信封；ts 为 Unix 秒级时间戳。"""

    id: str = Field(default_factory=lambda: uuid4().hex)
    type: str
    tenant_id: str = ""
    run_id: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    ts: float = Field(default_factory=time.time)


class EventSinkPort(Protocol):
    async def publish(self, event: EventEnvelope) -> None: ...
