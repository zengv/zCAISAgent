import asyncio

from tests.conftest import make_ctx, make_state, run_graph
from zcaisagent.ports.n2sql import N2SQLResult


class _FakeService:
    def __init__(self) -> None:
        self.questions: list[str] = []

    async def ask(self, question, auth):
        self.questions.append(question)
        return N2SQLResult(
            ok=True,
            answer="一共有 7 门课程。",
            sql="SELECT COUNT(*) AS total FROM courses",
            columns=["total"],
            rows=[{"total": 7}],
            scope="管理员",
        )


def test_data_question_routes_to_n2sql() -> None:
    ctx = make_ctx()
    service = _FakeService()
    ctx.n2sql = service
    state = make_state("一共有多少门课程？")
    result = asyncio.run(run_graph(ctx, state))

    assert service.questions == ["一共有多少门课程？"]
    assert result.status.value == "succeeded"
    assert result.answer is not None and "7" in result.answer.text
    assert result.last_node == "respond"
    assert result.analysis["n2sql"]["row_count"] == 1
    assert any(item.source_name == "mysql" for item in result.evidence)


def test_ranking_question_routes_to_n2sql() -> None:
    ctx = make_ctx()
    service = _FakeService()
    ctx.n2sql = service
    result = asyncio.run(run_graph(ctx, make_state("2025年语文排名前10名？")))

    assert service.questions == ["2025年语文排名前10名？"]
    assert result.last_node == "respond"
    assert result.analysis["n2sql"]["ok"] is True


def test_wenshu_prefix_routes_and_strips_trigger() -> None:
    ctx = make_ctx()
    service = _FakeService()
    ctx.n2sql = service
    result = asyncio.run(run_graph(ctx, make_state("问数：2025年语文排名前10名？")))

    assert service.questions == ["2025年语文排名前10名？"]
    assert result.last_node == "respond"


def test_knowledge_question_does_not_trigger_n2sql() -> None:
    ctx = make_ctx()
    ctx.n2sql = _FakeService()
    state = make_state("一线城市住宿标准是多少？")
    result = asyncio.run(run_graph(ctx, state))

    assert "data_query" not in result.analysis
    assert result.last_node in {"respond", "abstain"}
