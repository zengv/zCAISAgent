from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field

from zcaisagent.ports.security import AuthContext


class MemorySource(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    WEB = "web"
    DISTILL = "distill"


class MemoryQuery(BaseModel):
    tenant_id: str = "demo"
    user_id: str = ""
    text: str = ""
    limit: int = 5
    kinds: list[str] = Field(default_factory=list)


class MemoryItem(BaseModel):
    id: str
    kind: str = "semantic"
    content: str
    metadata: dict = Field(default_factory=dict)


class MemoryWrite(BaseModel):
    """带来源与风险标记的写入请求，用于写入策略判定。"""

    item: MemoryItem
    source: MemorySource = MemorySource.USER
    explicit: bool = False
    risk: str = "low"


class MemoryPort(Protocol):
    async def read(self, query: MemoryQuery, ctx: AuthContext) -> list[MemoryItem]: ...

    async def write(self, item: MemoryItem, ctx: AuthContext) -> None: ...

    async def write_batch(self, items: list[MemoryWrite], ctx: AuthContext) -> None: ...
