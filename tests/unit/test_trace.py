import asyncio

from tests.conftest import make_chunk, make_ctx, make_state, run_graph


def test_step_trace_records_input_output_timing(tmp_path) -> None:
    ctx = make_ctx([make_chunk("travel#0", "一线城市住宿标准为 800 元/晚。")], tmp_path)
    state = make_state("一线城市住宿标准是多少？")
    state = asyncio.run(run_graph(ctx, state))
    events = [e for e in ctx.trace.events(state.run_id) if e.type == "step.finished"]
    assert len(events) == state.step
    for event in events:
        assert event.node
        for key in ("duration_ms", "model", "tokens", "cost_usd", "input", "output"):
            assert key in event.data
        assert event.data["input"]["goal"] == state.goal
        assert event.data["output"]["status"]
    last = events[-1]
    assert last.data["duration_ms"] is not None
    assert last.data["model"] is not None


def test_verify_step_records_claim_decisions(tmp_path) -> None:
    ctx = make_ctx([make_chunk("travel#0", "一线城市住宿标准为 800 元/晚。")], tmp_path)
    state = asyncio.run(run_graph(ctx, make_state("一线城市住宿标准是多少？")))
    verify_event = next(
        event
        for event in ctx.trace.events(state.run_id)
        if event.node == "verify" and event.type == "step.finished"
    )
    notes = verify_event.data["output"]["notes"]
    assert any(note.startswith("verified:pass") for note in notes)
    assert any(note.startswith("claim:c0:") for note in notes)
