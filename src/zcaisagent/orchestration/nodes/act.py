from __future__ import annotations

import asyncio

from zcaisagent.capabilities.skills.registry import allowed_sources
from zcaisagent.capabilities.tools.gateway import to_evidence
from zcaisagent.domain.models import Plan, RunState, RunStatus
from zcaisagent.domain.patch import StatePatch
from zcaisagent.orchestration.actions.registry import ActionSpace, build_default_action_space
from zcaisagent.orchestration.runtime import RunContext
from zcaisagent.ports.retrieval import RetrieveRequest

# 计划步骤语义 → 动作空间条目名称。
_ACTION_KIND = {"tool": "tool_call"}


def _space(ctx: RunContext) -> ActionSpace:
    space = ctx.extra.get("actions")
    return space if isinstance(space, ActionSpace) else build_default_action_space()


def _mark(plan: Plan, step_id: str, status: str) -> Plan:
    steps = [
        step.model_copy(update={"status": status}) if step.id == step_id else step
        for step in plan.steps
    ]
    return plan.model_copy(update={"steps": steps})


async def _retrieve(query: str, state: RunState, ctx: RunContext) -> dict:
    request = RetrieveRequest(
        query=query,
        top_k=int(getattr(ctx.config, "top_k", 5)),
        tenant_id=state.tenant_id,
        acl_tokens=ctx.auth.acl_tokens,
    )
    retrievers = ctx.sources.list() if ctx.sources is not None else [ctx.retriever]
    whitelist = allowed_sources(state)
    if whitelist:
        retrievers = [item for item in retrievers if item.source_name in whitelist]

    async def call(retriever):
        return await asyncio.wait_for(
            retriever.retrieve(request, ctx.auth), timeout=ctx.node_timeout_s
        )

    results = await asyncio.gather(
        *(call(retriever) for retriever in retrievers), return_exceptions=True
    )
    items: list[dict] = []
    sources: list[str] = []
    for retriever, result in zip(retrievers, results):
        if isinstance(result, BaseException):
            sources.append(f"{retriever.source_name}:error")
            continue
        sources.append(
            f"{result.source_name}:{'degraded' if result.degraded else 'ok'}"
        )
        items.extend(item.model_dump(mode="json") for item in result.evidence)
    return {"items": items, "sources": sources}


async def _tool_call(
    pending, query: str, plan: Plan, state: RunState, ctx: RunContext
) -> StatePatch:
    if ctx.tools is None:
        return StatePatch(
            status=RunStatus.FAILED,
            error={"code": "ACTION_UNAVAILABLE", "message": "tool_call"},
        )
    name = pending.target or "kb_search"
    args = {**pending.args}
    args.setdefault("query", query)
    result = await ctx.tools.call(name, args, ctx.auth, state.run_id)
    evidence = to_evidence(result)
    items = [evidence.model_dump(mode="json")] if evidence else []
    status = "ok" if evidence else ("empty" if result.ok else "error")
    action = {
        "step_id": pending.id,
        "kind": "tool_call",
        "tool": name,
        "query": query,
        "status": status,
        "items": items,
        "sources": [f"{name}:{status}"],
        "error": result.error,
    }
    return StatePatch(
        plan=_mark(plan, pending.id, "done" if evidence else "failed"),
        analysis={"action": action},
        append_notes=[f"action:{pending.id}:tool_call:{len(items)}"],
    )


async def act_node(state: RunState, ctx: RunContext) -> StatePatch:
    plan = state.plan
    if plan is None or not plan.steps:
        return StatePatch(
            status=RunStatus.FAILED,
            error={"code": "EMPTY_PLAN", "message": "plan has no steps"},
        )
    pending = next((step for step in plan.steps if step.status == "pending"), None)
    if pending is None:
        return StatePatch(analysis={"action": {"status": "none"}})

    action_kind = _ACTION_KIND.get(pending.kind, pending.kind)
    spec = _space(ctx).get(action_kind)
    if spec is None or not spec.mvp:
        return StatePatch(
            status=RunStatus.FAILED,
            error={"code": "ACTION_UNAVAILABLE", "message": action_kind},
        )
    missing = _space(ctx).missing_scopes(action_kind, ctx.auth.scopes)
    if missing:
        return StatePatch(
            status=RunStatus.FAILED,
            error={
                "code": "ACTION_UNAUTHORIZED",
                "message": f"{action_kind} missing scopes: {','.join(missing)}",
            },
        )

    query = (pending.query or state.goal).strip()
    if pending.kind in {"retrieve", "tool"} and not query:
        return StatePatch(
            status=RunStatus.FAILED,
            error={"code": "ACTION_INVALID_ARGS", "message": "query is empty"},
        )

    if pending.kind == "tool":
        return await _tool_call(pending, query, plan, state, ctx)

    observation = await _retrieve(query, state, ctx)
    action = {
        "step_id": pending.id,
        "kind": pending.kind,
        "query": query,
        "status": "ok" if observation["items"] else "empty",
        **observation,
    }
    return StatePatch(
        plan=_mark(plan, pending.id, "done"),
        analysis={"action": action},
        append_notes=[
            f"action:{pending.id}:{pending.kind}:{len(observation['items'])}"
        ],
    )
