import asyncio

from tests.conftest import make_ctx, make_state
from zcaisagent.adapters.cache.memory import InMemoryCache
from zcaisagent.capabilities.retrieval.registry import SourceRegistry
from zcaisagent.domain.enums import SourceType
from zcaisagent.domain.models import Evidence
from zcaisagent.orchestration.nodes.retrieve import retrieve_node
from zcaisagent.ports.retrieval import RetrieveRequest, RetrieveResult
from zcaisagent.ports.security import AuthContext


class CountingRetriever:
    source_name = "counting"
    source_type = SourceType.DOCUMENT

    def __init__(self) -> None:
        self.calls = 0

    async def retrieve(
        self, req: RetrieveRequest, ctx: AuthContext
    ) -> RetrieveResult:
        self.calls += 1
        return RetrieveResult(
            source_name=self.source_name,
            evidence=[
                Evidence(
                    id="counting#0",
                    source_type=SourceType.DOCUMENT,
                    source_name=self.source_name,
                    title="测试文档",
                    content="一线城市住宿标准为 800 元/晚。",
                    score=0.9,
                    relevance=0.9,
                )
            ],
            latency_ms=7,
        )


def _ctx(retriever: CountingRetriever, cache: InMemoryCache, index_version: str = "v1"):
    ctx = make_ctx()
    registry = SourceRegistry()
    registry.register(retriever)
    ctx.sources = registry
    ctx.cache = cache
    ctx.extra = {"index_version": index_version}
    return ctx


def test_repeated_retrieval_hits_cache() -> None:
    retriever = CountingRetriever()
    ctx = _ctx(retriever, InMemoryCache())
    state = make_state("住宿标准是多少")

    first = asyncio.run(retrieve_node(state, ctx))
    second = asyncio.run(retrieve_node(state, ctx))

    assert retriever.calls == 1
    assert "retrieve_cache:hit:counting" in second.append_notes
    assert [item.id for item in first.append_evidence] == [
        item.id for item in second.append_evidence
    ]
    assert first.append_evidence == second.append_evidence


def test_cache_key_is_scoped_by_tenant_acl_and_index_version() -> None:
    retriever = CountingRetriever()
    ctx = _ctx(retriever, InMemoryCache())
    state = make_state("住宿标准是多少")

    asyncio.run(retrieve_node(state, ctx))
    assert retriever.calls == 1

    asyncio.run(retrieve_node(state, ctx))
    assert retriever.calls == 1

    other_tenant = state.model_copy(update={"tenant_id": "other"})
    asyncio.run(retrieve_node(other_tenant, ctx))
    assert retriever.calls == 2

    ctx.auth = ctx.auth.model_copy(update={"acl_tokens": ["finance"]})
    asyncio.run(retrieve_node(state, ctx))
    assert retriever.calls == 3

    ctx.auth = ctx.auth.model_copy(update={"acl_tokens": ["public"]})
    ctx.extra = {"index_version": "v2"}
    asyncio.run(retrieve_node(state, ctx))
    assert retriever.calls == 4


def test_replay_hit_rate_meets_m2_target() -> None:
    """重复回放 10 次（5 个唯一查询各 2 次），命中率应 ≥ 20%。"""
    retriever = CountingRetriever()
    cache = InMemoryCache()
    ctx = _ctx(retriever, cache)
    goals = [f"住宿标准问题 {index}" for index in range(5)]

    for goal in goals:
        asyncio.run(retrieve_node(make_state(goal), ctx))
    for goal in goals:
        asyncio.run(retrieve_node(make_state(goal), ctx))

    stats = cache.stats()
    assert retriever.calls == 5
    assert stats.hits == 5
    assert stats.hit_rate >= 0.2
