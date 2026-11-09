import asyncio

from tests.conftest import make_chunk, make_ctx, make_state, run_graph
from zcaisagent.domain.enums import AutonomyLevel, RunStatus


def test_l2_loop_is_bounded_to_two_subquestions() -> None:
    ctx = make_ctx(
        [
            make_chunk("kb#0", "一线城市住宿标准为 800 元/晚。"),
            make_chunk("kb#1", "年假天数为 5 天。"),
            make_chunk("kb#2", "加班需提前一天申请。"),
        ],
        top_k=3,
    )
    state = make_state(
        "住宿标准以及年假天数是多少？", AutonomyLevel.L2_STANDARD
    ).model_copy(update={"requested_level": AutonomyLevel.L2_STANDARD})

    state = asyncio.run(run_graph(ctx, state))

    assert state.plan is not None
    assert len(state.plan.steps) == 2
    assert state.status != RunStatus.BUDGET_EXCEEDED


def test_l3_runs_plan_act_observe_reflect_loop() -> None:
    ctx = make_ctx(
        [
            make_chunk("kb#0", "一线城市住宿标准为 800 元/晚。"),
            make_chunk("kb#1", "年假天数为 5 天。"),
        ],
        top_k=3,
    )
    state = make_state(
        "住宿标准以及年假天数是多少？", AutonomyLevel.L3_DEEP
    ).model_copy(update={"requested_level": AutonomyLevel.L3_DEEP})

    state = asyncio.run(run_graph(ctx, state))

    assert state.plan is not None
    assert len(state.plan.steps) >= 2
    assert {"kb#0", "kb#1"} <= {item.id for item in state.evidence}
    for prefix in ("plan:", "action:", "observe:", "reflect:complete"):
        assert any(note.startswith(prefix) for note in state.working_notes)
    assert state.status != RunStatus.BUDGET_EXCEEDED
