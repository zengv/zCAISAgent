from zcaisagent.domain.enums import RunStatus
from zcaisagent.domain.models import Answer, RunState
from zcaisagent.domain.patch import StatePatch
from zcaisagent.orchestration.runtime import RunContext

_REFUSAL_TEXT = {
    "privilege_escalation": (
        "该请求涉及访问其他租户、其他部门或他人的敏感数据，超出当前权限范围，已拒绝执行。"
        "请在自身权限范围内提问。"
    ),
    "destructive_action": (
        "该请求要求执行破坏性操作或绕过管控流程，已拒绝执行。如确有需要，请通过正式审批流程。"
    ),
    "injection": "该请求包含试图规避系统规则的指令，已拒绝执行。",
}


async def abstain_node(state: RunState, ctx: RunContext) -> StatePatch:
    sufficiency = state.analysis.get("sufficiency") or {}
    missing = sufficiency.get("missing_aspects") or []
    reason = str(state.analysis.get("abstain_reason") or "")
    if state.analysis.get("intent") == "不安全":
        category = str(state.analysis.get("unsafe_category") or "")
        text = _REFUSAL_TEXT.get(category, "该请求包含不安全或越权指令，已拒绝执行。")
        limitations = ["unsafe_input", *([category] if category else [])]
        abstain_reason = "unsafe_input"
    elif reason == "number_mismatch":
        # 与注入/越权拒答区分：属于"证据不足/无法确认"，须说明缺失项与建议来源。
        text = (
            "资料中的数值无法确认：回答涉及的关键数字或日期与制度原文不一致，"
            "补充检索与改写后仍无法消除差异。"
        )
        if missing:
            text += "缺少：" + "；".join(missing) + "。"
        text += "建议：核对制度原文中的数值或日期条款，或提供更具体的制度名称与条款范围。"
        limitations = ["unverifiable_numbers", *missing]
        abstain_reason = "number_mismatch"
    elif reason == "budget_insufficient":
        # 与"证据不足"区分：证据可能没问题，是剩余额度不足以完成进一步核对。
        text = "本次未能在预算内完成核对，因此暂不给出答案。"
        if missing:
            text += "尚待确认：" + "；".join(missing) + "。"
        text += "建议：缩小问题范围（指定制度名称或条款），或重试一次以获得更多核对轮次。"
        limitations = ["budget_insufficient", *missing]
        abstain_reason = "budget_insufficient"
    else:
        text = "未找到足够证据回答该问题。"
        if missing:
            text += "缺少：" + "；".join(missing) + "。"
        text += "建议：补充相关制度文档，或使用更具体的关键词（制度名称、时间范围、生效版本）。"
        limitations = ["evidence_insufficient", *missing]
        abstain_reason = "evidence_insufficient"
    return StatePatch(
        status=RunStatus.ABSTAINED,
        answer=Answer(
            text=text,
            abstained=True,
            confidence="low",
            limitations=limitations,
        ),
        analysis={"abstain_reason": abstain_reason},
        append_notes=["abstained"],
    )
