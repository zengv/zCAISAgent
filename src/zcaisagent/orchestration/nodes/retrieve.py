import asyncio

from zcaisagent.capabilities.retrieval.fusion import rrf_fuse
from zcaisagent.capabilities.retrieval.rewrite import rewrite_queries
from zcaisagent.capabilities.skills.registry import allowed_sources
from zcaisagent.domain.models import Evidence, RunState
from zcaisagent.domain.patch import StatePatch
from zcaisagent.orchestration.runtime import RunContext
from zcaisagent.ports.retrieval import RetrieveRequest


async def retrieve_node(state: RunState, ctx: RunContext) -> StatePatch:
    top_k = int(getattr(ctx.config, "top_k", 5))
    # 修复阶段的补检索使用改写后的 query，原始问题保留在 state.goal。
    retry_query = state.analysis.get("retry_query")
    if retry_query:
        variants = [{"strategy": "retry", "query": retry_query}]
    else:
        # 多轮追问先用上下文补全的 query 检索，原始 goal 保留在 state.goal。
        base = state.analysis.get("followup_query") or state.goal
        variants = rewrite_queries(
            base,
            state.analysis.get("complexity", "simple"),
            state.analysis.get("intent", "查事实"),
        )

    retrievers = ctx.sources.list() if ctx.sources is not None else [ctx.retriever]
    whitelist = allowed_sources(state)
    if whitelist:
        retrievers = [item for item in retrievers if item.source_name in whitelist]

    async def call(retriever, query: str):
        request = RetrieveRequest(
            query=query,
            top_k=top_k,
            tenant_id=state.tenant_id,
            acl_tokens=ctx.auth.acl_tokens,
        )
        return await asyncio.wait_for(
            retriever.retrieve(request, ctx.auth), timeout=ctx.node_timeout_s
        )

    calls = [(variant, retriever) for variant in variants for retriever in retrievers]
    results = await asyncio.gather(
        *(call(retriever, variant["query"]) for variant, retriever in calls),
        return_exceptions=True,
    )

    ranked_lists: list[list[Evidence]] = []
    notes: list[str] = []
    degraded: set[str] = set()
    for (variant, retriever), result in zip(calls, results):
        strategy = variant["strategy"]
        if isinstance(result, BaseException):
            degraded.add(retriever.source_name)
            notes.append(f"retrieve_error:{retriever.source_name}:{strategy}")
            continue
        if result.degraded:
            degraded.add(result.source_name)
        ranked_lists.append(result.evidence)
        notes.append(
            f"retrieve:{result.source_name}:{strategy}:{len(result.evidence)}"
        )

    for variant in variants:
        if variant["strategy"] not in {"none", "retry"}:
            notes.append(f"rewrite:{variant['strategy']}")

    fused = rrf_fuse(ranked_lists)

    if ctx.guardrail is not None:
        quarantined: list[str] = []
        for evidence in fused:
            decision = await ctx.guardrail.check_evidence(evidence, ctx.auth)
            if decision.allowed:
                continue
            quarantined.append(evidence.id)
        if quarantined:
            rejected = set(quarantined)
            fused = [evidence for evidence in fused if evidence.id not in rejected]
            notes.append("evidence_quarantined:" + ",".join(sorted(rejected)))

    if degraded:
        notes.append("retrieve_degraded:" + ",".join(sorted(degraded)))
    return StatePatch(append_evidence=fused, append_notes=notes)
