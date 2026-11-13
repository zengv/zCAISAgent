import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from zcaisagent.capabilities.tools.gateway import ToolGateway
from zcaisagent.domain.enums import ApprovalStatus, RunStatus
from zcaisagent.domain.models import RunState
from zcaisagent.domain.patch import StatePatch
from zcaisagent.orchestration.graph import GraphSpec
from zcaisagent.orchestration.policies.budget import BudgetPolicy
from zcaisagent.orchestration.reducers import apply_patch
from zcaisagent.orchestration.telemetry import emit
from zcaisagent.ports.cache import CachePort
from zcaisagent.ports.checkpoint import CheckpointPort
from zcaisagent.ports.evaluation import AnswerVerifierPort, EvaluatorPort
from zcaisagent.ports.events import EventSinkPort
from zcaisagent.ports.health import HealthPort, HealthStatus
from zcaisagent.ports.llm import LLMPort
from zcaisagent.ports.memory import MemoryPort
from zcaisagent.ports.n2sql import N2SQLPort
from zcaisagent.ports.retrieval import RetrieverPoolPort, RetrieverPort
from zcaisagent.ports.security import AuditPort, AuthContext, GuardrailPort
from zcaisagent.ports.skills import SkillRegistryPort
from zcaisagent.ports.trace import TracePort


@dataclass
class RunContext:
    llm: LLMPort
    retriever: RetrieverPort
    checkpoint: CheckpointPort
    trace: TracePort
    auth: AuthContext
    config: Any = None
    sources: RetrieverPoolPort | None = None
    reranker: Any = None
    # 证据评分/充分性策略；None 时 grade 节点回退到本地策略函数。
    evaluator: EvaluatorPort | None = None
    # 答案级语义校验（NLI judge）；None 时 verify 节点使用词面近似校验。
    verifier: AnswerVerifierPort | None = None
    health: HealthPort | None = None
    guardrail: GuardrailPort | None = None
    audit: AuditPort | None = None
    skills: SkillRegistryPort | None = None
    tools: ToolGateway | None = None
    memory: MemoryPort | None = None
    # 智能问数；None 表示未启用，聊天链路不会触发数据查询。
    n2sql: N2SQLPort | None = None
    # 结果缓存；None 表示不缓存（默认）。
    cache: CachePort | None = None
    # 事件投递出口；None 表示不投递（默认）。
    events: EventSinkPort | None = None
    node_timeout_s: float = 30.0
    clock: Any = None
    # 编排层事件出口（如 SSE）；None 时保持纯批量执行。
    emit: Callable[[str, dict], Awaitable[None]] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class Runtime:
    async def execute(
        self, graph: GraphSpec, state: RunState, ctx: RunContext
    ) -> RunState:
        await emit(
            ctx,
            state,
            "run.started",
            data={"level": state.level.value, "goal_len": len(state.goal)},
        )
        while state.status == RunStatus.RUNNING:
            budget_reason = BudgetPolicy.check(state)
            if budget_reason is not None:
                await emit(ctx, state, "budget.exceeded", data={"reason": budget_reason})
                state = apply_patch(
                    state,
                    StatePatch(
                        status=RunStatus.BUDGET_EXCEEDED,
                        error={"code": "BUDGET_EXCEEDED", "message": budget_reason},
                    ),
                )
                break

            node_name = self._next_node(graph, state)
            if node_name == state.resume_node:
                # resume_node 只生效一次：执行前清空，避免重复进入同一中断点。
                state = state.model_copy(update={"resume_node": None})
            if node_name in graph.terminals:
                state = apply_patch(
                    state, StatePatch(status=graph.terminals[node_name])
                )
                await ctx.checkpoint.save(state)
                break
            if node_name not in graph.nodes:
                state = apply_patch(
                    state,
                    StatePatch(
                        status=RunStatus.FAILED,
                        error={"code": "DEAD_END", "message": node_name},
                    ),
                )
                break

            before_tokens = state.budget.used_tokens
            before_cost = state.budget.used_cost_usd
            before_evidence = len(state.evidence)
            ctx.trace.start_step(state.step, node_name, state.run_id)
            await emit(ctx, state, "step.started", node=node_name)
            if ctx.emit is not None:
                await ctx.emit(
                    "stage",
                    {"node": node_name, "step": state.step, "phase": "start"},
                )
            try:
                patch = await graph.nodes[node_name](state, ctx)
                if ctx.health is not None:
                    ctx.health.report("runtime", HealthStatus.OK)
            except Exception as exc:  # noqa: BLE001 - runtime boundary
                if ctx.health is not None:
                    ctx.health.report("runtime", HealthStatus.DOWN, str(exc))
                patch = StatePatch(
                    status=RunStatus.FAILED,
                    error={"code": "NODE_FAILED", "message": str(exc)},
                )

            patch.last_node = node_name
            if patch.incr_step == 0:
                patch.incr_step = 1
            input_summary = {
                "goal": state.goal[:120],
                "level": state.level.value,
                "step": state.step,
                "evidence": before_evidence,
            }
            state = apply_patch(state, patch)
            await ctx.checkpoint.save(state)
            if ctx.emit is not None:
                await ctx.emit(
                    "stage",
                    {"node": node_name, "step": state.step, "phase": "end"},
                )
            ctx.trace.end_step(
                state,
                {
                    "node": node_name,
                    "model": getattr(ctx.config, "default_model_name", "fake"),
                    "tokens": state.budget.used_tokens - before_tokens,
                    "cost_usd": round(state.budget.used_cost_usd - before_cost, 6),
                    "input": input_summary,
                    "output": {
                        "status": state.status.value,
                        "answer": state.answer.text[:160] if state.answer else None,
                        "evidence": len(state.evidence),
                        "notes": list(patch.append_notes),
                        "error": state.error,
                        # 中间产物：本节点产出的结构化分析（如 n2sql 的 SQL/列/行数）。
                        "analysis": patch.analysis,
                        # 中间产物：本节点新增的证据（含检索/工具原始结果摘要）。
                        "evidence_added": [
                            {
                                "id": item.id,
                                "source_type": item.source_type.value,
                                "source_name": item.source_name,
                                "title": item.title,
                                "snippet": item.snippet,
                            }
                            for item in patch.append_evidence
                        ],
                    },
                },
            )

            if (
                state.status == RunStatus.AWAITING_USER
                and state.pending_approval is not None
            ):
                await emit(
                    ctx,
                    state,
                    "approval.requested",
                    node=node_name,
                    data={
                        "approval_id": state.pending_approval.id,
                        "tool": state.pending_approval.tool,
                    },
                )
                if ctx.emit is not None:
                    await ctx.emit(
                        "approval_required",
                        {
                            "run_id": state.run_id,
                            "status": state.status.value,
                            "pending_approval": state.pending_approval.model_dump(
                                mode="json"
                            ),
                        },
                    )
                await ctx.checkpoint.save(state)
                break

        # finished_ts 必须在最后一次落盘前赋值，否则 checkpoint 里永远是 0。
        await emit(
            ctx,
            state,
            "run.finished",
            data={
                "status": state.status.value,
                "steps": state.step,
                "actions": state.budget.used_actions,
                "tokens": state.budget.used_tokens,
                "cost_usd": round(state.budget.used_cost_usd, 6),
            },
        )
        state.finished_ts = time.time()
        await ctx.checkpoint.save(state)
        return state

    async def resume(
        self, graph: GraphSpec, state: RunState, ctx: RunContext
    ) -> RunState:
        """审批决策后从 resume_node 继续；预算与步数沿用原状态。"""
        if state.status != RunStatus.AWAITING_USER:
            return state
        approval = state.pending_approval
        decided = approval is not None and approval.status in {
            ApprovalStatus.APPROVED,
            ApprovalStatus.REJECTED,
        }
        if not decided and not state.resume_node:
            return state
        if approval is not None:
            await emit(
                ctx,
                state,
                "approval.resolved",
                data={"approval_id": approval.id, "status": approval.status.value},
            )
        resumed = state.model_copy(update={"status": RunStatus.RUNNING})
        return await self.execute(graph, resumed, ctx)

    @staticmethod
    def _next_node(graph: GraphSpec, state: RunState) -> str:
        if state.resume_node:
            return state.resume_node
        return graph.next_node(state)
