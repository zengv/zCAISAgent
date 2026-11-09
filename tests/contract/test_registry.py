import asyncio

from zcaisagent.capabilities.retrieval.registry import SourceRegistry
from zcaisagent.capabilities.retrieval.sources.inmemory import InMemoryRetriever
from zcaisagent.domain.enums import RunStatus
from zcaisagent.ports.retrieval import RetrieveRequest
from tests.conftest import make_chunk, make_ctx, make_state, run_graph


class NamedRetriever(InMemoryRetriever):
    def __init__(self, name: str) -> None:
        super().__init__()
        self.source_name = name


def test_registry_rejects_duplicate_names() -> None:
    registry = SourceRegistry()
    registry.register(NamedRetriever("a"))
    try:
        registry.register(NamedRetriever("a"))
    except ValueError as exc:
        assert "already registered" in str(exc)
    else:
        raise AssertionError("duplicate source was accepted")


def test_registry_lists_sources_in_priority_order() -> None:
    registry = SourceRegistry()
    registry.register(NamedRetriever("low"), priority=200)
    registry.register(NamedRetriever("high"), priority=10)
    assert [source.source_name for source in registry.list()] == ["high", "low"]


def test_retrieve_node_merges_multiple_sources() -> None:
    first = NamedRetriever("kb")
    first.add([make_chunk("kb#0", "一线城市住宿标准为 800 元/晚。")])
    second = NamedRetriever("web")
    second.add([make_chunk("web#0", "一线城市住宿标准为 800 元/晚。")])

    registry = SourceRegistry()
    registry.register(first)
    registry.register(second)

    ctx = make_ctx()
    ctx.sources = registry
    state = asyncio.run(run_graph(ctx, make_state("一线城市住宿标准是多少？")))

    assert state.status == RunStatus.SUCCEEDED
    # 内容相同只保留一条，但来源出处必须全部保留。
    assert len(state.evidence) == 1
    provenance = state.evidence[0].metadata["sources"]
    assert {item["source_name"] for item in provenance} == {"kb", "web"}


def test_registry_partial_failure_degrades() -> None:
    class BrokenRetriever(NamedRetriever):
        async def retrieve(self, req: RetrieveRequest, ctx):
            raise RuntimeError("boom")

    good = NamedRetriever("good")
    good.add([make_chunk("good#0", "年假为 5 天。")])
    broken = BrokenRetriever("broken")

    registry = SourceRegistry()
    registry.register(good)
    registry.register(broken)
    ctx = make_ctx()
    ctx.sources = registry
    state = asyncio.run(run_graph(ctx, make_state("年假有几天？")))

    assert any(note.startswith("retrieve_error:broken") for note in state.working_notes)
    assert state.evidence
