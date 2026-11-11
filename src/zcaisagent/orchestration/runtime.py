import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from zcaisagent.capabilities.tools.gateway import ToolGateway
from zcaisagent.domain.models import RunState, RunStatus
from zcaisagent.domain.patch import StatePatch
from zcaisagent.orchestration.graph import GraphSpec
from zcaisagent.orchestration.policies.budget import BudgetPolicy
from zcaisagent.orchestration.reducers import apply_patch
from zcaisagent.ports.checkpoint import CheckpointPort
from zcaisagent.ports.health import HealthPort, HealthStatus
from zcaisagent.ports.llm import LLMPort
from zcaisagent.ports.retrieval import RetrieverPoolPort, RetrieverPort
from zcaisagent.ports.security import AuthContext, GuardrailPort
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
    health: HealthPort | None = None
    guardrail: GuardrailPort | None = None
    skills: SkillRegistryPort | None = None
    tools: ToolGateway | None = None
    node_timeout_s: float = 30.0
    clock: Any = None
    # 编排层事件出口（如 SSE）；None 时保持纯批量执行。
    emit: Callable[[str, dict], Awaitable[None]] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class Runtime:
    async def execute(
        self, graph: GraphSpec, state: RunState, ctx: RunContext
    ) -> RunState:
        while state.status == RunStatus.RUNNING:
            budget_reason = BudgetPolicy.check(state)
            if budget_reason is not None:
                state = apply_patch(
                    state,
                    StatePatch(
                        status=RunStatus.BUDGET_EXCEEDED,
                        error={"code": "BUDGET_EXCEEDED", "message": budget_reason},
                    ),
                )
                break

            node_name = graph.next_node(state)
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
                    },
                },
            )

        await ctx.checkpoint.save(state)
        state.finished_ts = time.time()
        return state
