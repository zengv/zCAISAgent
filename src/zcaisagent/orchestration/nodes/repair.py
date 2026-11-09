from zcaisagent.domain.enums import Support
from zcaisagent.domain.models import Claim, RunState
from zcaisagent.domain.patch import StatePatch
from zcaisagent.orchestration.runtime import RunContext

_REPAIR_LIMIT = 2
_NUMBER_MISMATCH = "number_mismatch"
# 一轮修复所需的最少节点步数（不含本节点）：
# retrieve 分支 = retrieve, grade, synthesize, verify, respond；
# rewrite 分支 = synthesize, verify, respond。
_REPAIR_STEPS_RETRIEVE = 5
_REPAIR_STEPS_REWRITE = 3


def _has_number_mismatch(claims: list[Claim]) -> bool:
    return any(
        claim.support != Support.UNSUPPORTED and _NUMBER_MISMATCH in (claim.note or "")
        for claim in claims
    )


def _budget_short_for(state: RunState, action: str) -> bool:
    """判断剩余预算是否够走完这一轮修复。

    只预判可确定的步数与调用数；token/成本取决于实际生成用量、无法预估，
    仍由运行时兜底，避免拿额度上限估算而导致过度拒答。
    """
    needed = _REPAIR_STEPS_RETRIEVE if action == "retrieve" else _REPAIR_STEPS_REWRITE
    remaining_steps = state.budget.max_steps - state.budget.used_steps
    if remaining_steps < needed + 1:  # +1 为本节点自身
        return True
    return state.budget.used_llm_calls + 1 > state.budget.max_llm_calls


async def repair_node(state: RunState, ctx: RunContext) -> StatePatch:
    """定向修复：补检索 → 改写生成 → 降置信度并标注 → 拒答。"""
    round_no = state.repair_round
    baseline = state.analysis.get("evidence_baseline")
    evidence_count = len(state.evidence)
    verdict = state.verdict
    claims = state.answer.claims if state.answer else []
    any_supported = any(claim.support != Support.UNSUPPORTED for claim in claims)
    number_conflict = _has_number_mismatch(claims)

    if verdict is None:
        action = "retrieve" if round_no == 0 else "abstain"
    elif verdict.passed:
        action = "respond"
    elif round_no >= _REPAIR_LIMIT:
        # 方案 A：数值被明确判定不一致且修复失败时，不再降级交付错误答案，直接拒答。
        action = "abstain" if number_conflict else (
            "degrade" if any_supported else "abstain"
        )
    elif round_no == 0:
        action = "retrieve"
    else:
        action = "rewrite"

    # 无进展检测：补检索后证据数未增加则停止重试。
    if action == "retrieve" and baseline is not None and evidence_count <= baseline:
        action = "abstain"

    # 预算预检：修复本身要再花若干节点与一次生成调用，剩余额度不够时
    # 直接拒答，避免走到一半被运行时判成 budget_exceeded（既无答案也无拒答理由）。
    budget_short = False
    if action in ("retrieve", "rewrite") and _budget_short_for(state, action):
        action = "abstain"
        budget_short = True

    analysis: dict = {"repair_action": action, "repair_round": round_no + 1}
    if action == "abstain":
        if budget_short:
            analysis["abstain_reason"] = "budget_insufficient"
        elif number_conflict:
            analysis["abstain_reason"] = "number_mismatch"
    if action == "retrieve":
        analysis["evidence_baseline"] = evidence_count
        retry_query = state.goal
        for claim in claims:
            if claim.support != Support.SUPPORTED:
                retry_query = f"{state.goal} {claim.text[:60]}"
                break
        analysis["retry_query"] = retry_query
    elif action == "rewrite":
        analysis["strict"] = True

    return StatePatch(
        incr_repair=1,
        analysis=analysis,
        append_notes=[f"repair:{action}"],
    )
