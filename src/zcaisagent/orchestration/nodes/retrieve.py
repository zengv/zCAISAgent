import asyncio
import hashlib
import json

from zcaisagent.capabilities.retrieval.fusion import rrf_fuse, source_weight
from zcaisagent.capabilities.retrieval.rewrite import hyde_variant, rewrite_queries
from zcaisagent.capabilities.skills.registry import allowed_sources
from zcaisagent.domain.models import Evidence, RunState
from zcaisagent.domain.patch import StatePatch
from zcaisagent.orchestration.runtime import RunContext
from zcaisagent.orchestration.telemetry import emit
from zcaisagent.ports.retrieval import RetrieveRequest, RetrieveResult
from zcaisagent.ports.security import AuditEvent


def _cache_key(
    state: RunState, ctx: RunContext, source_name: str, top_k: int, query: str
) -> str:
    """检索缓存键：租户 + ACL 摘要 + 源 + top_k + 索引版本 + 归一化 query。"""
    acl_digest = hashlib.sha256(
        json.dumps(sorted(ctx.auth.acl_tokens), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    normalized = " ".join(query.lower().split())
    index_version = ctx.extra.get("index_version", "")
    return (
        f"retrieval:{state.tenant_id}:{acl_digest}:{source_name}:"
        f"{top_k}:{index_version}:{normalized}"
    )


async def retrieve_node(state: RunState, ctx: RunContext) -> StatePatch:
    top_k = int(getattr(ctx.config, "top_k", 5))
    # 修复阶段的补检索使用改写后的 query，原始问题保留在 state.goal。
    retry_query = state.analysis.get("retry_query")
    budget = state.budget
    notes: list[str] = []
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
        # HyDE 只用于开放问题；假设性答案参与召回与 RRF，原 query 仍排第一。
        if (
            getattr(ctx.config, "hyde_enabled", False)
            and state.analysis.get("complexity") in {"open"}
        ):
            response = await hyde_variant(
                ctx.llm,
                base,
                timeout_s=ctx.node_timeout_s,
                max_tokens=int(getattr(ctx.config, "hyde_max_tokens", 256)),
            )
            if response is not None:
                variants.append({"strategy": "hyde", "query": response.text.strip()})
                budget = budget.model_copy(
                    update={
                        "used_llm_calls": budget.used_llm_calls + 1,
                        "used_tokens": (
                            budget.used_tokens + response.tokens_in + response.tokens_out
                        ),
                        "used_tokens_in": budget.used_tokens_in + response.tokens_in,
                        "used_tokens_out": budget.used_tokens_out + response.tokens_out,
                        "used_cached_tokens": (
                            budget.used_cached_tokens + response.cached_tokens
                        ),
                        "used_cost_usd": budget.used_cost_usd + response.cost_usd,
                    }
                )
                notes.append("hyde:applied")
            else:
                notes.append("hyde:unavailable")

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
        cache = ctx.cache
        cache_key = (
            _cache_key(state, ctx, retriever.source_name, top_k, query)
            if cache is not None
            else None
        )
        if cache is not None and cache_key is not None:
            try:
                cached = await cache.get(cache_key)
            except Exception:  # noqa: BLE001 - 缓存故障不阻断检索
                cached = None
                notes.append("retrieve_cache:error")
            if cached is not None:
                try:
                    result = RetrieveResult(
                        source_name=str(cached["source_name"]),
                        evidence=[
                            Evidence.model_validate(item)
                            for item in cached["evidence"]
                        ],
                        latency_ms=0,
                    )
                except Exception:  # noqa: BLE001 - 缓存内容损坏时回落真实检索
                    notes.append("retrieve_cache:error")
                else:
                    notes.append(f"retrieve_cache:hit:{retriever.source_name}")
                    return result

        result = await asyncio.wait_for(
            retriever.retrieve(request, ctx.auth), timeout=ctx.node_timeout_s
        )
        # 只缓存成功结果；降级结果下一次仍走真实检索。
        if cache is not None and cache_key is not None and not result.degraded:
            try:
                await cache.set(
                    cache_key,
                    {
                        "source_name": result.source_name,
                        "evidence": [
                            item.model_dump(mode="json") for item in result.evidence
                        ],
                        "latency_ms": result.latency_ms,
                    },
                )
            except Exception:  # noqa: BLE001 - 缓存故障不阻断检索
                notes.append("retrieve_cache:error")
        return result

    calls = [(variant, retriever) for variant in variants for retriever in retrievers]
    results = await asyncio.gather(
        *(call(retriever, variant["query"]) for variant, retriever in calls),
        return_exceptions=True,
    )

    ranked_lists: list[list[Evidence]] = []
    degraded: set[str] = set()
    for (variant, retriever), result in zip(calls, results, strict=False):
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

    fused = rrf_fuse(
        ranked_lists,
        weights=[source_weight(retriever.source_name) for _, retriever in calls],
    )

    # 融合后 LLM 相关性重排：rerank 分写入 evidence.relevance 与
    # analysis.rerank_scores，grade 节点用它替代词法相关性；失败则回退。
    reranker = getattr(ctx, "reranker", None)
    rerank_scores: dict[str, float] | None = None
    if reranker is not None and fused:
        rerank_query = retry_query or state.analysis.get("followup_query") or state.goal
        try:
            scores = await reranker.rerank(rerank_query, [item.content for item in fused])
            rerank_scores = {
                item.id: round(score, 4) for item, score in zip(fused, scores, strict=False)
            }
            fused = [
                item.model_copy(update={"relevance": round(score, 4)})
                for item, score in zip(fused, scores, strict=False)
            ]
            notes.append(f"rerank:applied:{len(fused)}")
        except Exception:  # noqa: BLE001 - rerank 不可用时回退词法相关性
            rerank_scores = None
            notes.append("rerank:unavailable")

    if ctx.guardrail is not None:
        quarantined: list[tuple[Evidence, str | None, str | None]] = []
        for evidence in fused:
            decision = await ctx.guardrail.check_evidence(evidence, ctx.auth)
            if decision.allowed:
                continue
            quarantined.append(
                (evidence, decision.category, decision.details.get("marker"))
            )
        if quarantined:
            rejected = {evidence.id for evidence, _, _ in quarantined}
            fused = [evidence for evidence in fused if evidence.id not in rejected]
            notes.append("evidence_quarantined:" + ",".join(sorted(rejected)))
            if ctx.audit is not None:
                for evidence, category, marker in quarantined:
                    try:
                        await ctx.audit.append(
                            AuditEvent(
                                action="guardrail.evidence_quarantined",
                                actor=ctx.auth.user_id,
                                tenant_id=ctx.auth.tenant_id,
                                run_id=state.run_id,
                                detail={
                                    "evidence_id": evidence.id,
                                    "source_name": evidence.source_name,
                                    "category": category,
                                    "marker": marker,
                                },
                            )
                        )
                    except Exception:  # noqa: BLE001 - 审计失败不阻断 Run
                        pass

    if degraded:
        notes.append("retrieve_degraded:" + ",".join(sorted(degraded)))
    source_hits: dict[str, dict] = {}
    for (_variant, retriever), result in zip(calls, results, strict=False):
        if isinstance(result, BaseException):
            continue
        agg = source_hits.setdefault(
            retriever.source_name, {"hits": 0, "degraded": False}
        )
        agg["hits"] += len(result.evidence)
        agg["degraded"] = agg["degraded"] or result.degraded
    for source, agg in source_hits.items():
        await emit(
            ctx,
            state,
            "retrieval.performed",
            node="retrieve",
            data={
                "source": source,
                "top_k": top_k,
                "hits": agg["hits"],
                "degraded": agg["degraded"],
            },
        )
    if rerank_scores is not None:
        await emit(
            ctx,
            state,
            "rerank.performed",
            node="retrieve",
            data={"count": len(rerank_scores)},
        )
    patch = StatePatch(
        append_evidence=fused,
        append_notes=notes,
        budget=budget,
        incr_actions=1,
    )
    if rerank_scores:
        patch.analysis = {"rerank_scores": rerank_scores}
    return patch
