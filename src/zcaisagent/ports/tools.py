from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from zcaisagent.ports.security import AuthContext


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict = Field(default_factory=dict)
    output_schema: dict | None = None
    risk_level: Literal["read", "write", "destructive"] = "read"
    scopes: list[str] = Field(default_factory=list)
    timeout_s: int = 20
    idempotent: bool = False
    requires_approval: bool = False
    max_output_chars: int = 8_000


class ToolResult(BaseModel):
    tool: str
    ok: bool
    output: Any = None
    error: str | None = None
    latency_ms: int = 0


class ToolPort(Protocol):
    spec: ToolSpec

    async def invoke(self, args: dict, ctx: AuthContext) -> ToolResult: ...
