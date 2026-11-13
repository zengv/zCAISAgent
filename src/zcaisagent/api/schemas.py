import json
import time
from datetime import datetime

from pydantic import BaseModel, Field

from zcaisagent.domain.models import RunState
from zcaisagent.ports.security import AuthContext


class RunRequest(BaseModel):
    goal: str = Field(min_length=1)
    thread_id: str | None = None
    tenant_id: str | None = None
    user_id: str = "dev"
    level: str | None = None


class ApprovalDecision(BaseModel):
    """审批/拒绝请求体；body 可空。"""

    reason: str = ""
    actor: str | None = None


class CitationOut(BaseModel):
    evidence_id: str
    quote: str | None = None
    confidence: float = 0.0


class ApprovalOut(BaseModel):
    """HITL 卡片：只暴露参数键与前 80 字符，避免泄漏完整敏感值。"""

    id: str
    tool: str
    risk_level: str
    args_summary: dict[str, str] = Field(default_factory=dict)
    status: str
    requested_at: datetime


class MetricsOut(BaseModel):
    elapsed_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_total: int = 0
    token_budget: int = 0
    token_percent: float = 0.0
    cached_tokens: int = 0
    cache_hit_rate: float = 0.0
    llm_calls: int = 0


class RunResponse(BaseModel):
    run_id: str
    thread_id: str
    status: str
    stage: str
    answer: str
    confidence: str
    abstained: bool
    citations: list[CitationOut] = Field(default_factory=list)
    evidence_count: int = 0
    steps: int = 0
    mandate_id: str | None = None
    limitations: list[str] = Field(default_factory=list)
    metrics: MetricsOut = Field(default_factory=MetricsOut)
    approval: ApprovalOut | None = None


def _args_summary(args: dict) -> dict[str, str]:
    summary: dict[str, str] = {}
    for key, value in args.items():
        text = (
            value
            if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False, default=str)
        )
        summary[str(key)] = text[:80]
    return summary


def build_approval(state: RunState) -> ApprovalOut | None:
    approval = state.pending_approval
    if approval is None:
        return None
    return ApprovalOut(
        id=approval.id,
        tool=approval.tool,
        risk_level=approval.risk_level,
        args_summary=_args_summary(approval.args),
        status=approval.status.value,
        requested_at=approval.requested_at,
    )


def build_metrics(state: RunState) -> MetricsOut:
    """汇总一次 run 的耗时 / token / 缓存命中数据。"""
    budget = state.budget
    elapsed_s = (
        max(0.0, (state.finished_ts or time.time()) - state.created_ts)
        if state.created_ts
        else 0.0
    )
    return MetricsOut(
        elapsed_ms=int(elapsed_s * 1000),
        tokens_in=budget.used_tokens_in,
        tokens_out=budget.used_tokens_out,
        tokens_total=budget.used_tokens,
        token_budget=budget.max_tokens,
        token_percent=(
            round(budget.used_tokens / budget.max_tokens * 100, 2)
            if budget.max_tokens
            else 0.0
        ),
        cached_tokens=budget.used_cached_tokens,
        cache_hit_rate=(
            round(budget.used_cached_tokens / budget.used_tokens_in * 100, 2)
            if budget.used_tokens_in
            else 0.0
        ),
        llm_calls=budget.used_llm_calls,
    )


class N2SQLRequest(BaseModel):
    question: str = Field(min_length=1)
    user_id: str = "dev"
    tenant_id: str | None = None


class N2SQLResponse(BaseModel):
    question: str
    answer: str
    sql: str
    columns: list[str] = Field(default_factory=list)
    rows: list[dict] = Field(default_factory=list)
    row_count: int = 0
    scope: str = ""
    latency_ms: int = 0


class N2SQLExportRequest(BaseModel):
    """把已经返回给前端的问数结果导出为 Excel，避免重复调用模型与数据库。"""

    question: str = ""
    columns: list[str] = Field(default_factory=list)
    rows: list[dict] = Field(default_factory=list)
    user_id: str = "dev"
    tenant_id: str | None = None


def auth_meta(request: RunRequest) -> dict:
    return {
        "tenant_id": request.tenant_id or "",
        "user_id": request.user_id,
    }


def ensure_tenant(auth: AuthContext, request: RunRequest) -> str:
    return request.tenant_id or auth.tenant_id
