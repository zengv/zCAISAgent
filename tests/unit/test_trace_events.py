import asyncio

from tests.conftest import make_chunk, make_ctx, make_state, run_graph


def test_fine_grained_events_emitted(tmp_path) -> None:
    ctx = make_ctx([make_chunk("travel#0", "一线城市住宿标准为 800 元/晚。")], tmp_path)
    state = asyncio.run(run_graph(ctx, make_state("一线城市住宿标准是多少？")))
    types = {event.type for event in ctx.trace.events(state.run_id)}
    for expected in (
        "run.started",
        "step.started",
        "step.finished",
        "retrieval.performed",
        "evidence.graded",
        "answer.verified",
        "run.finished",
    ):
        assert expected in types, f"missing event: {expected}"
