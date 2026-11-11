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


class ApprovalRequest(BaseModel):
    """工具调用需要人工审批时返回的结构化请求。"""

    tool: str
    args: dict = Field(default_factory=dict)
    risk_level: Literal["read", "write", "destructive"] = "write"
    reason: str = ""
    scopes: list[str] = Field(default_factory=list)


class ToolCallDecision(BaseModel):
    """ToolGateway 四段校验的最终决策。"""

    allowed: bool = True
    reason: str = ""
    approval_required: bool = False
    approval_id: str | None = None


class ToolResult(BaseModel):
    tool: str
    ok: bool
    output: Any = None
    error: str | None = None
    latency_ms: int = 0
    # 审批中断：ok=False 且 approval_required=True 时由 Runtime 进入 AWAITING_USER。
    approval_required: bool = False
    approval_id: str | None = None
    # 输出命中注入检测时隔离，不进入 evidence。
    quarantined: bool = False


class ToolPort(Protocol):
    spec: ToolSpec

    async def invoke(self, args: dict, ctx: AuthContext) -> ToolResult: ...
