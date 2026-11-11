import time

from zcaisagent.domain.enums import AutonomyLevel
from zcaisagent.domain.models import Budget, RunState

_PROFILES: dict[AutonomyLevel, tuple[int, int, int, float, int]] = {
    # 步数按"节点执行次数"计。L1=16 覆盖最坏修复路径：
    # analyze,route,retrieve,grade,synthesize,verify,repair,retrieve,grade,
    # synthesize,verify,repair,synthesize,verify,repair,respond。
    # L2=22 覆盖"2 子问题通用循环 + 2 轮修复 + 降级收尾"：
    # analyze,route,plan,(act,observe,reflect)×2,grade,synthesize,verify,
    # repair,retrieve,grade,synthesize,verify,repair,synthesize,verify,
    # repair,respond。
    # L3=32 覆盖"5 子问题通用循环 + 2 轮修复 + 降级收尾"。
    # 第 2 项为 max_actions（有效动作次数硬上限）：L0 直答 0~1、L1 快路径最多
    # retrieve×3（首发 + 2 轮补检索）、L2 2 子问题 + 修复、L3 5 子问题 + 修复。
    AutonomyLevel.L0_DIRECT: (4, 1, 4_000, 0.002, 10),
    AutonomyLevel.L1_FAST: (16, 3, 20_000, 0.008, 60),
    AutonomyLevel.L2_STANDARD: (22, 8, 60_000, 0.05, 40),
    AutonomyLevel.L3_DEEP: (32, 15, 180_000, 0.20, 120),
}


class BudgetPolicy:
    @staticmethod
    def initial(level: AutonomyLevel, now: float | None = None) -> Budget:
        max_steps, max_actions, max_tokens, max_cost, max_wall = _PROFILES[level]
        now = time.time() if now is None else now
        return Budget(
            max_steps=max_steps,
            max_actions=max_actions,
            max_tokens=max_tokens,
            max_cost_usd=max_cost,
            max_wall_time_s=max_wall,
            deadline_ts=now + max_wall,
        )

    @staticmethod
    def check(state: RunState, now: float | None = None) -> str | None:
        now = time.time() if now is None else now
        budget = state.budget
        if budget.used_steps >= budget.max_steps:
            return "max_steps"
        if budget.max_actions and budget.used_actions >= budget.max_actions:
            return "max_actions"
        if budget.used_tokens >= budget.max_tokens:
            return "max_tokens"
        if budget.used_cost_usd >= budget.max_cost_usd:
            return "max_cost_usd"
        if budget.deadline_ts and now >= budget.deadline_ts:
            return "max_wall_time_s"
        if budget.used_llm_calls >= budget.max_llm_calls:
            return "max_llm_calls"
        if state.step >= budget.max_steps:
            return "max_steps"
        return None
