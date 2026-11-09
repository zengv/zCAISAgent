from typing import Protocol

from pydantic import BaseModel, Field

from zcaisagent.ports.security import AuthContext


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


class MemoryPort(Protocol):
    async def read(self, query: MemoryQuery, ctx: AuthContext) -> list[MemoryItem]: ...

    async def write(self, item: MemoryItem, ctx: AuthContext) -> None: ...
