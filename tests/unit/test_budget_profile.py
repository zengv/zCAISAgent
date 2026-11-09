from zcaisagent.domain.enums import AutonomyLevel
from zcaisagent.orchestration.policies.budget import BudgetPolicy
from tests.conftest import make_state


def test_l1_step_budget_covers_worst_case_repair_path() -> None:
    # 最坏路径 16 个节点：6 个主流程 + 补检索 3 个 + 改写 3 个 + 收尾 4 个。
    state = make_state("一线城市住宿标准是多少？", AutonomyLevel.L1_FAST)
    assert state.budget.max_steps == 16
    state = state.model_copy(update={"step": 15})
    assert BudgetPolicy.check(state) is None
    state = state.model_copy(update={"step": 16})
    assert BudgetPolicy.check(state) == "max_steps"


def test_l1_wall_budget_allows_three_model_rounds() -> None:
    assert BudgetPolicy.initial(AutonomyLevel.L1_FAST).max_wall_time_s == 60


def test_step_budget_increases_with_autonomy_level() -> None:
    # L2 覆盖 2 子问题通用循环 + 2 轮修复；L3 覆盖 5 子问题循环 + 2 轮修复。
    levels = [
        AutonomyLevel.L0_DIRECT,
        AutonomyLevel.L1_FAST,
        AutonomyLevel.L2_STANDARD,
        AutonomyLevel.L3_DEEP,
    ]
    steps = [BudgetPolicy.initial(level).max_steps for level in levels]
    assert steps == [4, 16, 22, 32]
    assert steps == sorted(steps)
