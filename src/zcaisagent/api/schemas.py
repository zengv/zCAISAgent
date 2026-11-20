import json
import time
from datetime import datetime

from pydantic import BaseModel, Field

from zcaisagent.domain.enums import RunStatus
from zcaisagent.domain.models import RunState
from zcaisagent.domain.stage import ticket_stage
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


class ClarifyQuestion(BaseModel):
    question: str
    options: list[str] = Field(default_factory=list)


class ClarifyOut(BaseModel):
    questions: list[ClarifyQuestion] = Field(default_factory=list)
    reason: str = ""


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
    # 澄清追问：仅在 AWAITING_USER 且分析阶段产出有效问题时返回。
    clarify: ClarifyOut | None = None


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


def build_clarify(state: RunState) -> ClarifyOut | None:
    """把分析阶段的澄清数据清洗为对外结构；无有效题目返回 None。"""
    if state.status != RunStatus.AWAITING_USER:
        return None
    raw = state.analysis.get("clarify")
    if not isinstance(raw, dict):
        return None

    questions: list[ClarifyQuestion] = []
    for item in raw.get("questions") or []:
        if not isinstance(item, dict):
            continue
        question = item.get("question")
        if not isinstance(question, str) or not question.strip():
            continue
        options: list[str] = []
        for option in item.get("options") or []:
            if not isinstance(option, str):
                continue
            text = option.strip()
            # 去空并去重，最多保留 4 个候选。
            if text and text not in options:
                options.append(text)
        options = options[:4]
        # 少于 2 个候选的题目无法提供有效选择，直接丢弃。
        if len(options) < 2:
            continue
        questions.append(
            ClarifyQuestion(question=question.strip(), options=options)
        )
    # 规格要求一次最多追问 2 个问题，超出部分直接丢弃。
    questions = questions[:2]
    if not questions:
        return None

    reason = raw.get("reason")
    return ClarifyOut(
        questions=questions,
        reason=reason if isinstance(reason, str) else "",
    )


class ThreadMessageOut(BaseModel):
    """会话内的一轮问答；用于刷新页面后回放历史。"""

    run_id: str
    created_ts: float = 0.0
    goal: str
    answer: str = ""
    status: str
    stage: str
    confidence: str = "low"
    abstained: bool = False
    citations: list[CitationOut] = Field(default_factory=list)
    clarify: ClarifyOut | None = None


class ThreadHistoryOut(BaseModel):
    thread_id: str
    messages: list[ThreadMessageOut] = Field(default_factory=list)
    has_more: bool = False


def build_thread_message(state: RunState) -> ThreadMessageOut:
    answer = state.answer
    return ThreadMessageOut(
        run_id=state.run_id,
        created_ts=state.created_ts,
        goal=state.goal,
        answer=answer.text if answer else "",
        status=state.status.value,
        stage=ticket_stage(state).value,
        confidence=answer.confidence if answer else "low",
        abstained=answer.abstained if answer else False,
        citations=[
            CitationOut(
                evidence_id=citation.evidence_id,
                quote=citation.quote,
                confidence=citation.confidence,
            )
            for citation in (answer.citations if answer else [])
        ],
        clarify=build_clarify(state),
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
