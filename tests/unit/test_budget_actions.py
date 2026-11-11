import asyncio

from tests.conftest import make_ctx, make_state
from zcaisagent.domain.enums import AutonomyLevel, RunStatus
from zcaisagent.domain.patch import StatePatch
from zcaisagent.orchestration.graph import Edge, GraphSpec
from zcaisagent.orchestration.policies.budget import BudgetPolicy
from zcaisagent.orchestration.reducers import apply_patch
from zcaisagent.orchestration.runtime import Runtime


def test_max_actions_profile_per_level() -> None:
    assert BudgetPolicy.initial(AutonomyLevel.L0_DIRECT).max_actions == 1
    assert BudgetPolicy.initial(AutonomyLevel.L1_FAST).max_actions == 3
    assert BudgetPolicy.initial(AutonomyLevel.L2_STANDARD).max_actions == 8
    assert BudgetPolicy.initial(AutonomyLevel.L3_DEEP).max_actions == 15


def test_incr_actions_accumulates_via_reducer() -> None:
    state = make_state("测试")
    assert state.budget.used_actions == 0
    state = apply_patch(state, StatePatch(incr_actions=2))
    assert state.budget.used_actions == 2
    state = apply_patch(state, StatePatch(incr_actions=1))
    assert state.budget.used_actions == 3


def test_check_returns_max_actions() -> None:
    state = make_state("测试")
    state = apply_patch(state, StatePatch(incr_actions=state.budget.max_actions))
    # 截止时间设为未来，确保命中动作上限而非墙钟
    assert BudgetPolicy.check(state, now=state.budget.deadline_ts - 1) == "max_actions"


def test_runtime_stops_when_actions_exceeded() -> None:
    async def _act(state, ctx):  # noqa: ANN001 - 测试节点
        return StatePatch(incr_actions=1)

    ctx = make_ctx()
    graph = GraphSpec(nodes={"loop": _act}, edges=[Edge("loop", "loop")], entry="loop")
    state = make_state("测试")
    state = state.model_copy(
        update={"budget": state.budget.model_copy(update={"max_actions": 2})}
    )

    result = asyncio.run(Runtime().execute(graph, state, ctx))
    assert result.status == RunStatus.BUDGET_EXCEEDED
    assert result.budget.used_actions == 2
    assert result.error and result.error["code"] == "BUDGET_EXCEEDED"
