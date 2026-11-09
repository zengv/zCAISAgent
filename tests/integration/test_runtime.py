import asyncio

from zcaisagent.domain.enums import RunStatus
from tests.conftest import make_chunk, make_ctx, make_state, run_graph


def test_runtime_retrieves_and_answers() -> None:
    ctx = make_ctx([
        make_chunk("travel#0", "一线城市（北京、上海、广州、深圳）住宿标准为 800 元/晚。"),
    ])
    state = asyncio.run(run_graph(ctx, make_state("一线城市住宿标准是多少？")))
    assert state.status == RunStatus.SUCCEEDED
    assert state.evidence
    assert state.answer is not None
    assert "800" in state.answer.text
    assert state.answer.citations
    assert state.verdict and state.verdict.passed
    assert state.step >= 4


def test_runtime_direct_answer_for_chitchat() -> None:
    ctx = make_ctx()
    state = asyncio.run(run_graph(ctx, make_state("你好")))
    assert state.status == RunStatus.SUCCEEDED
    assert state.answer is not None
    assert not state.evidence


def test_runtime_abstains_without_evidence() -> None:
    ctx = make_ctx([
        make_chunk("travel#0", "一线城市住宿标准为 800 元/晚。"),
    ])
    state = asyncio.run(run_graph(ctx, make_state("公司股票代码是多少？")))
    assert state.status == RunStatus.ABSTAINED
    assert state.answer is not None and state.answer.abstained
