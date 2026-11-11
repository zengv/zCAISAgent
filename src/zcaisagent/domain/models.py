from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from zcaisagent.domain.enums import (
    ApprovalStatus,
    AutonomyLevel,
    RunStatus,
    SourceType,
    Support,
)
from zcaisagent.domain.mandate import Mandate


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class Citation(BaseModel):
    evidence_id: str
    quote: str | None = None
    start: int | None = None
    end: int | None = None
    confidence: float = 0.0


class Evidence(BaseModel):
    id: str
    source_type: SourceType = SourceType.DOCUMENT
    source_name: str
    title: str | None = None
    uri: str | None = None
    content: str
    snippet: str | None = None
    score: float = 0.0
    relevance: float = 0.0
    authority: Literal["primary", "secondary", "unknown"] = "unknown"
    freshness: float = 0.0
    consistency: float = 0.0
    final_score: float = 0.0
    grade: Literal["keep", "drop", "conflict"] = "keep"
    published_at: datetime | None = None
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    version: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    citation: Citation | None = None
    # 父子块：子块命中后可回填所属父块正文。
    parent_content: str | None = None


class Claim(BaseModel):
    id: str
    text: str
    evidence_ids: list[str] = Field(default_factory=list)
    support: Support = Support.UNSUPPORTED
    note: str | None = None


class Answer(BaseModel):
    text: str
    claims: list[Claim] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "low"
    abstained: bool = False
    limitations: list[str] = Field(default_factory=list)


class PlanStep(BaseModel):
    id: str
    kind: Literal["retrieve", "tool", "reason", "ask_user"] = "retrieve"
    target: str = ""
    query: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending", "done", "failed", "skipped"] = "pending"


class Plan(BaseModel):
    steps: list[PlanStep] = Field(default_factory=list)
    version: int = 1


class Budget(BaseModel):
    max_steps: int = 8
    max_llm_calls: int = 12
    max_tokens: int = 60_000
    max_cost_usd: float = 0.15
    max_wall_time_s: int = 90
    used_steps: int = 0
    used_llm_calls: int = 0
    used_tokens: int = 0
    used_tokens_in: int = 0
    used_tokens_out: int = 0
    used_cached_tokens: int = 0
    used_cost_usd: float = 0.0
    deadline_ts: float = 0.0


class Verdict(BaseModel):
    passed: bool = False
    faithfulness: float = 0.0
    citation_support: float = 0.0
    issues: list[str] = Field(default_factory=list)
    suggested_action: Literal[
        "respond", "repair_retrieve", "repair_rewrite", "abstain"
    ] = "respond"


class Skill(BaseModel):
    """技能包：指令 + 源/工具白名单 + 策略收紧，不做提权。"""

    name: str
    version: str
    description: str = ""
    instructions: str = ""
    trigger_keywords: tuple[str, ...] = ()
    allowed_sources: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    required_scopes: tuple[str, ...] = ()
    min_citation_support: float | None = None


class PendingApproval(BaseModel):
    """待人工审批的工具调用请求；字段全给默认值以便旧状态反序列化。"""

    id: str = ""
    run_id: str = ""
    step_id: str = ""
    tool: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    risk_level: Literal["read", "write", "destructive"] = "write"
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    decided_at: datetime | None = None
    decided_by: str | None = None
    reason: str = ""
    result_digest: str | None = None


class ToolInvocation(BaseModel):
    """一次工具调用的审计明细。"""

    id: str = ""
    tool: str = ""
    args_hash: str = ""
    risk_level: Literal["read", "write", "destructive"] = "read"
    status: Literal["ok", "error", "approval_required", "rejected"] = "ok"
    latency_ms: int = 0
    result_digest: str | None = None
    approval_id: str | None = None
    error: str | None = None


class RunState(BaseModel):
    run_id: str
    thread_id: str
    tenant_id: str
    user_id: str
    goal: str
    created_ts: float = 0.0
    finished_ts: float = 0.0
    level: AutonomyLevel = AutonomyLevel.L1_FAST
    requested_level: AutonomyLevel | None = None
    mandate: Mandate | None = None
    analysis: dict[str, Any] = Field(default_factory=dict)
    messages: list[Message] = Field(default_factory=list)
    plan: Plan | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    working_notes: list[str] = Field(default_factory=list)
    answer: Answer | None = None
    verdict: Verdict | None = None
    budget: Budget = Field(default_factory=Budget)
    step: int = 0
    last_node: str | None = None
    repair_round: int = 0
    status: RunStatus = RunStatus.RUNNING
    error: dict[str, Any] | None = None
    # HITL：非 None 时 Run 停在 AWAITING_USER，恢复后从 resume_node 继续。
    pending_approval: PendingApproval | None = None
    resume_node: str | None = None
    tool_invocations: list[ToolInvocation] = Field(default_factory=list)
    schema_version: int = 1


class Document(BaseModel):
    doc_id: str
    title: str
    content: str
    uri: str | None = None
    tenant_id: str = "demo"
    acl: list[str] = Field(default_factory=lambda: ["public"])
    published_at: datetime | None = None


class Chunk(BaseModel):
    id: str
    doc_id: str
    title: str
    content: str
    index: int
    tenant_id: str = "demo"
    acl: list[str] = Field(default_factory=lambda: ["public"])
    uri: str | None = None
    published_at: datetime | None = None
    version: str | None = None
    # 父子块：父块不单独入库，子块携带父文与父 id，检索时按需扩展。
    parent_content: str | None = None
    parent_id: str | None = None
