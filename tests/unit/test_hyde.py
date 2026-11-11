import asyncio
from dataclasses import replace

from zcaisagent.adapters.llm.fake import FakeLLM
from zcaisagent.orchestration.nodes.retrieve import retrieve_node
from tests.conftest import make_chunk, make_ctx, make_state


class _BrokenLLM(FakeLLM):
    async def complete(self, request):
        raise RuntimeError("llm down")


def _hyde_ctx():
    ctx = make_ctx(
        [make_chunk("kb#0", "差旅报销需在出差结束后 15 日内提交。")], top_k=3
    )
    ctx.config = replace(ctx.config, hyde_enabled=True)
    return ctx


def _open_state(goal: str):
    return make_state(goal).model_copy(
        update={"analysis": {"complexity": "open", "intent": "查事实"}}
    )


def test_hyde_appends_variant_after_original() -> None:
    ctx = _hyde_ctx()
    state = _open_state("如何制定差旅报销流程？")

    patch = asyncio.run(retrieve_node(state, ctx))

    notes = patch.append_notes or []
    assert "hyde:applied" in notes
    original = [index for index, note in enumerate(notes) if ":none:" in note]
    hyde = [index for index, note in enumerate(notes) if ":hyde:" in note]
    assert original and hyde
    assert max(original) < min(hyde)
    assert patch.budget is not None
    assert patch.budget.used_llm_calls == state.budget.used_llm_calls + 1


def test_hyde_unavailable_keeps_original_only() -> None:
    ctx = _hyde_ctx()
    ctx.llm = _BrokenLLM()
    state = _open_state("如何制定差旅报销流程？")

    patch = asyncio.run(retrieve_node(state, ctx))

    notes = patch.append_notes or []
    assert "hyde:unavailable" in notes
    assert not any(":hyde:" in note for note in notes)
    assert patch.budget is not None
    assert patch.budget.used_llm_calls == state.budget.used_llm_calls


def test_hyde_skipped_when_disabled() -> None:
    ctx = make_ctx([make_chunk("kb#0", "年假 5 天。")], top_k=3)
    state = _open_state("如何制定差旅报销流程？")

    patch = asyncio.run(retrieve_node(state, ctx))

    notes = patch.append_notes or []
    assert not any(note.startswith("hyde:") for note in notes)
