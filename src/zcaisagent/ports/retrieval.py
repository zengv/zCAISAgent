from typing import Protocol, Any

from pydantic import BaseModel, Field

from zcaisagent.domain.enums import SourceType
from zcaisagent.domain.models import Evidence
from zcaisagent.ports.security import AuthContext


class RetrieveRequest(BaseModel):
    query: str
    top_k: int = 5
    tenant_id: str = "demo"
    acl_tokens: list[str] = Field(default_factory=lambda: ["public"])
    filters: dict[str, Any] = Field(default_factory=dict)


class RetrieveResult(BaseModel):
    source_name: str
    evidence: list[Evidence] = Field(default_factory=list)
    degraded: bool = False
    latency_ms: int = 0


class RetrieverPort(Protocol):
    source_name: str
    source_type: SourceType

    async def retrieve(self, req: RetrieveRequest, ctx: AuthContext) -> RetrieveResult: ...


class RetrieverPoolPort(Protocol):
    def list(self, enabled_only: bool = True) -> list[RetrieverPort]: ...
