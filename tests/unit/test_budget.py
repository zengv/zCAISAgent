from zcaisagent.orchestration.policies.budget import BudgetPolicy
from tests.conftest import make_state


def test_budget_exceeded_by_steps() -> None:
    state = make_state("测试")
    state.step = state.budget.max_steps
    assert BudgetPolicy.check(state, now=1_000_000.0) == "max_steps"


def test_budget_exceeded_by_deadline() -> None:
    state = make_state("测试")
    assert BudgetPolicy.check(state, now=state.budget.deadline_ts + 1) == "max_wall_time_s"


def test_budget_ok() -> None:
    state = make_state("测试")
    assert BudgetPolicy.check(state, now=1_000_000.0) is None
