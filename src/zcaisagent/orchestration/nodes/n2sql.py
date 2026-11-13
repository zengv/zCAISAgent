from __future__ import annotations

import hashlib
import json

from zcaisagent.domain.enums import SourceType, Support
from zcaisagent.domain.models import (
    Answer,
    Citation,
    Claim,
    Evidence,
    Message,
    RunState,
    Verdict,
)
from zcaisagent.domain.patch import StatePatch
from zcaisagent.orchestration.runtime import RunContext
from zcaisagent.orchestration.telemetry import emit
from zcaisagent.ports.n2sql import N2SQLResult


def _charge(state: RunState, result: N2SQLResult):
    """把智能问数的两次 LLM 调用记入预算（无调用时保持原值）。"""
    if not (result.llm_calls or result.tokens_in or result.tokens_out):
        return state.budget
    return state.budget.model_copy(
        update={
            "used_llm_calls": state.budget.used_llm_calls + result.llm_calls,
            "used_tokens": state.budget.used_tokens
            + result.tokens_in
            + result.tokens_out,
            "used_tokens_in": state.budget.used_tokens_in + result.tokens_in,
            "used_tokens_out": state.budget.used_tokens_out + result.tokens_out,
            "used_cost_usd": state.budget.used_cost_usd + result.cost_usd,
        }
    )


def _failed_patch(result: N2SQLResult, budget) -> StatePatch:
    text = result.error_message or "智能问数执行失败。"
    answer = Answer(
        text=text,
        abstained=True,
        confidence="low",
        limitations=[result.error_code] if result.error_code else [],
    )
    return StatePatch(
        answer=answer,
        verdict=Verdict(
            passed=True,
            faithfulness=0.0,
            citation_support=0.0,
            suggested_action="respond",
        ),
        budget=budget,
        analysis={"n2sql": {"ok": False, "error": result.error_code}},
        append_message=Message(role="assistant", content=text),
        append_notes=[f"n2sql:failed:{result.error_code}"],
        incr_actions=1,
    )


async def n2sql_node(state: RunState, ctx: RunContext) -> StatePatch:
    service = getattr(ctx, "n2sql", None)
    if service is None:
        return _failed_patch(
            N2SQLResult(
                ok=False,
                error_code="N2SQL_DISABLED",
                error_message="智能问数未启用或数据库未配置。",
            ),
            state.budget,
        )

    question = str(state.analysis.get("data_query_text") or state.goal)
    result = await service.ask(question, ctx.auth)
    budget = _charge(state, result)
    # 逐次 LLM 调用留痕：prompt 与模型原始响应原样落轨迹（失败时也可回溯）。
    for record in result.llm_trace:
        await emit(ctx, state, "llm.called", node="n2sql", data=record)
    if not result.ok:
        return _failed_patch(result, budget)

    # 结构化结果单独推给 SSE 消费方（聊天页图表/Excel），不写入状态，避免 checkpoint 膨胀。
    if ctx.emit is not None:
        await ctx.emit(
            "data_query",
            {
                "ok": True,
                "sql": result.sql,
                "columns": result.columns,
                "rows": result.rows,
                "row_count": len(result.rows),
                "row_limit": result.row_limit,
                "scope": result.scope,
                "latency_ms": result.latency_ms,
            },
        )

    rows_json = json.dumps(result.rows, ensure_ascii=False, default=str)
    digest = hashlib.sha256((result.sql + rows_json).encode("utf-8")).hexdigest()[:10]
    evidence = Evidence(
        id=f"sql:mysql:{digest}",
        source_type=SourceType.SQL,
        source_name="mysql",
        title="智能问数查询结果",
        content=(
            f"SQL: {result.sql}\n数据权限: {result.scope}\n"
            f"查询结果: {rows_json[:4000]}"
        ),
        snippet=rows_json[:160],
        score=1.0,
        relevance=1.0,
        authority="primary",
        final_score=1.0,
    )
    answer = Answer(
        text=result.answer,
        claims=[
            Claim(
                id="c0",
                text=result.answer,
                evidence_ids=[evidence.id],
                support=Support.SUPPORTED,
            )
        ],
        citations=[
            Citation(
                evidence_id=evidence.id,
                quote=evidence.content[:160],
                confidence=1.0,
            )
        ],
        confidence="high",
    )
    return StatePatch(
        append_evidence=[evidence],
        answer=answer,
        verdict=Verdict(
            passed=True,
            faithfulness=1.0,
            citation_support=1.0,
            suggested_action="respond",
        ),
        budget=budget,
        analysis={
            "n2sql": {
                "ok": True,
                "sql": result.sql,
                "columns": result.columns,
                "row_count": len(result.rows),
                "scope": result.scope,
                "latency_ms": result.latency_ms,
            },
            "sufficiency": {
                "coverage": 1.0,
                "missing_aspects": [],
                "decision": "sufficient",
            },
        },
        append_message=Message(role="assistant", content=result.answer),
        append_notes=[f"n2sql:ok:rows={len(result.rows)}", f"n2sql:scope:{result.scope}"],
        incr_actions=1,
    )
