import asyncio

from tests.conftest import make_chunk, make_ctx, make_state, run_graph
from zcaisagent.capabilities.retrieval.registry import SourceRegistry
from zcaisagent.capabilities.retrieval.sources.inmemory import InMemoryRetriever
from zcaisagent.capabilities.skills.registry import build_default_skill_registry


class WebRetriever(InMemoryRetriever):
    source_name = "web"


def test_enterprise_skill_matches_and_restricts_source_whitelist() -> None:
    ctx = make_ctx(
        [make_chunk("kb#0", "年假天数为 5 天。")],
        top_k=3,
        skills=build_default_skill_registry(),
    )
    web = WebRetriever()
    web.add([make_chunk("web#0", "年假天数为 10 天。")])
    sources = SourceRegistry()
    sources.register(ctx.retriever)
    sources.register(web)
    ctx.sources = sources

    state = asyncio.run(run_graph(ctx, make_state("年假天数是多少？")))

    skill = state.analysis.get("skill") or {}
    assert skill.get("matched") is True
    assert skill.get("name") == "enterprise_knowledge_qa"
    assert any(
        note == "skill:enterprise_knowledge_qa:keyword:年假"
        for note in state.working_notes
    )
    assert {item.id for item in state.evidence} == {"kb#0"}
