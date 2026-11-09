import asyncio

from tests.conftest import make_ctx, make_state
from zcaisagent.domain.enums import RunStatus, Support
from zcaisagent.domain.models import Answer, Claim, RunState, Verdict
from zcaisagent.orchestration.nodes.abstain import abstain_node
from zcaisagent.orchestration.nodes.repair import repair_node


def _state(
    note: str,
    support: Support = Support.PARTIAL,
    passed: bool = False,
    repair_round: int = 2,
) -> RunState:
    state = make_state("试用期 6 个月，转正申请最晚何时提交？")
    return state.model_copy(
        update={
            "repair_round": repair_round,
            "answer": Answer(
                text="最晚应在 2026 年 8 月 1 日前提交。",
                claims=[
                    Claim(
                        id="c0",
                        text="最晚应在 2026 年 8 月 1 日前提交。",
                        evidence_ids=["ev1"],
                        support=support,
                        note=note,
                    )
                ],
            ),
            "verdict": Verdict(
                passed=passed,
                citation_support=0.5,
                faithfulness=0.9,
                issues=["number_mismatch"] if note else [],
                suggested_action="abstain" if not passed else "respond",
            ),
        }
    )


def test_number_mismatch_after_repair_abstains() -> None:
    state = _state("overlap=0.51;number_mismatch")
    patch = asyncio.run(repair_node(state, make_ctx()))
    assert patch.analysis["repair_action"] == "abstain"
    assert patch.analysis["abstain_reason"] == "number_mismatch"
    assert "repair:abstain" in patch.append_notes


def test_partial_without_number_conflict_still_degrades() -> None:
    # 仅覆盖率不足、没有数字冲突时仍按原策略降级作答，改动面不扩大。
    state = _state("overlap=0.40")
    patch = asyncio.run(repair_node(state, make_ctx()))
    assert patch.analysis["repair_action"] == "degrade"
    assert "abstain_reason" not in patch.analysis


def test_passed_verdict_responds_after_repair() -> None:
    # 修复成功（校验通过）时正常作答，不进入拒答分支。
    state = _state("overlap=0.80", support=Support.SUPPORTED, passed=True)
    patch = asyncio.run(repair_node(state, make_ctx()))
    assert patch.analysis["repair_action"] == "respond"
    assert "abstain_reason" not in patch.analysis


def test_unsupported_claim_is_not_treated_as_number_conflict() -> None:
    # 文本无有效证据（UNSUPPORTED）时，即使 note 残留数字标记也按证据不足拒答，
    # 避免 no_answer 题被误归因为"数值无法确认"。
    state = _state(
        "no_valid_evidence;number_mismatch",
        support=Support.UNSUPPORTED,
    )
    patch = asyncio.run(repair_node(state, make_ctx()))
    assert patch.analysis["repair_action"] == "abstain"
    assert "abstain_reason" not in patch.analysis


def test_budget_short_abstains_before_repair() -> None:
    # L1 步数预算 16：rewrite 分支还需 synthesize/verify/respond 3 步 + 本节点 1 步，
    # 只剩 3 步时不足以完成，直接拒答而不是走到一半变成 budget_exceeded。
    state = _state("overlap=0.40", repair_round=1)
    state = state.model_copy(
        update={"budget": state.budget.model_copy(update={"used_steps": 13})}
    )
    patch = asyncio.run(repair_node(state, make_ctx()))
    assert patch.analysis["repair_action"] == "abstain"
    assert patch.analysis["abstain_reason"] == "budget_insufficient"
    assert "repair:abstain" in patch.append_notes


def test_budget_enough_still_rewrites() -> None:
    state = _state("overlap=0.40", repair_round=1)
    patch = asyncio.run(repair_node(state, make_ctx()))
    assert patch.analysis["repair_action"] == "rewrite"
    assert "abstain_reason" not in patch.analysis


def test_budget_short_llm_calls_abstains() -> None:
    # 调用次数只剩 0 次时同样无法完成改写生成。
    state = _state("overlap=0.40", repair_round=1)
    state = state.model_copy(
        update={
            "budget": state.budget.model_copy(
                update={"used_llm_calls": state.budget.max_llm_calls}
            )
        }
    )
    patch = asyncio.run(repair_node(state, make_ctx()))
    assert patch.analysis["abstain_reason"] == "budget_insufficient"


def test_budget_abstain_text_is_distinct() -> None:
    state = make_state("试用期 6 个月，转正申请最晚何时提交？").model_copy(
        update={
            "analysis": {
                "abstain_reason": "budget_insufficient",
                "sufficiency": {"missing_aspects": ["制度原文中的转正期限"]},
            },
            "answer": Answer(
                text="最晚应在 2026 年 8 月 1 日前提交。",
                claims=[Claim(id="c0", text="最晚应在 2026 年 8 月 1 日前提交。")],
            ),
        }
    )
    patch = asyncio.run(abstain_node(state, make_ctx()))
    assert patch.status == RunStatus.ABSTAINED
    assert patch.answer is not None
    assert "预算" in patch.answer.text
    assert "制度原文中的转正期限" in patch.answer.text
    assert patch.answer.limitations[0] == "budget_insufficient"
    # 不得与"证据不足"文案混同。
    assert "未找到足够证据" not in patch.answer.text
    assert patch.analysis["abstain_reason"] == "budget_insufficient"


def test_number_mismatch_abstain_text_is_distinct() -> None:
    state = make_state("试用期 6 个月，转正申请最晚何时提交？").model_copy(
        update={
            "analysis": {
                "abstain_reason": "number_mismatch",
                "sufficiency": {"missing_aspects": ["制度原文中的转正期限"]},
            },
            "answer": Answer(
                text="最晚应在 2026 年 8 月 1 日前提交。",
                claims=[Claim(id="c0", text="最晚应在 2026 年 8 月 1 日前提交。")],
            ),
        }
    )
    patch = asyncio.run(abstain_node(state, make_ctx()))
    assert patch.status == RunStatus.ABSTAINED
    assert patch.answer is not None
    assert "数值无法确认" in patch.answer.text
    assert "制度原文中的转正期限" in patch.answer.text
    assert "建议" in patch.answer.text
    assert patch.answer.limitations[0] == "unverifiable_numbers"
    # 与注入/越权拒答区分：不得出现权限类文案。
    assert "权限范围" not in patch.answer.text
    assert patch.analysis["abstain_reason"] == "number_mismatch"
