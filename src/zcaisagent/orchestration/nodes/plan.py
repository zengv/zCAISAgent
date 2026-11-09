from __future__ import annotations

from zcaisagent.capabilities.retrieval.rewrite import rewrite_queries
from zcaisagent.domain.enums import AutonomyLevel
from zcaisagent.domain.models import Plan, PlanStep, RunState
from zcaisagent.domain.patch import StatePatch
from zcaisagent.orchestration.runtime import RunContext

# 子问题上限受档位步数预算约束：L2 循环每子问题消耗 3 步（act/observe/reflect）。
_MAX_STEPS: dict[AutonomyLevel, int] = {
    AutonomyLevel.L2_STANDARD: 2,
    AutonomyLevel.L3_DEEP: 5,
}

# MVP 只读工具；后续从工具注册表按需选择。
_DEFAULT_TOOL = "kb_search"


def _subqueries(state: RunState) -> list[str]:
    complexity = state.analysis.get("complexity", "simple")
    base = state.analysis.get("followup_query") or state.goal
    if complexity == "simple":
        return [base]
    variants = rewrite_queries(
        base, complexity, state.analysis.get("intent", "查事实")
    )
    queries = [
        variant["query"]
        for variant in variants
        if variant["strategy"].startswith(("multi_", "decompose_"))
    ]
    deduped: list[str] = []
    for query in queries:
        if query and query not in deduped:
            deduped.append(query)
    limit = _MAX_STEPS.get(state.level, 5)
    return deduped[:limit] or [state.goal]


async def plan_node(state: RunState, ctx: RunContext) -> StatePatch:
    queries = _subqueries(state)
    version = (state.plan.version if state.plan else 0) + 1
    if state.analysis.get("needs_tool"):
        steps = [
            PlanStep(
                id=f"s{index}",
                kind="tool",
                target=_DEFAULT_TOOL,
                query=query,
                args={"query": query},
            )
            for index, query in enumerate(queries, start=1)
        ]
    else:
        steps = [
            PlanStep(id=f"s{index}", kind="retrieve", target="all", query=query)
            for index, query in enumerate(queries, start=1)
        ]
    plan = Plan(steps=steps, version=version)
    return StatePatch(
        plan=plan,
        analysis={"plan_meta": {"version": version, "steps": len(steps)}},
        append_notes=[f"plan:v{version}:{len(steps)}"],
    )
