import asyncio

import pytest

from tests.conftest import make_state
from zcaisagent.domain.models import Answer, Claim, Evidence
from zcaisagent.orchestration.nodes.verify import verify_node


def _verify(goal: str, evidence: Evidence, claim_text: str):
    claim = Claim(id="c1", text=claim_text, evidence_ids=[evidence.id])
    state = make_state(goal).model_copy(
        update={
            "evidence": [evidence],
            "answer": Answer(text=claim_text, claims=[claim]),
        }
    )
    return asyncio.run(verify_node(state, None))


def test_derived_amount_is_accepted() -> None:
    # 15 = 24 - 9；3750 = 6000 * (24 - 9) / 24，均可由证据数字复算。
    evidence = Evidence(
        id="ev1",
        source_name="training_contract",
        title="培训协议",
        content="培训费用为 6000 元，服务期 24 个月；已履行 9 个月，未履行部分按比例赔偿。",
    )
    patch = _verify(
        "培训费 6000 元、服务期 24 个月，已履行 9 个月后离职，应赔偿多少元？",
        evidence,
        "服务期 24 个月，已履行 9 个月，未履行 15 个月，应赔偿 3750 元。",
    )
    assert patch.verdict.citation_support == 1.0
    assert "derived:" in patch.answer.claims[0].note


def test_fabricated_amount_still_fails() -> None:
    evidence = Evidence(
        id="ev1",
        source_name="training_contract",
        title="培训协议",
        content="培训费用为 6000 元，服务期 24 个月；已履行 9 个月，未履行部分按比例赔偿。",
    )
    patch = _verify(
        "培训费 6000 元、服务期 24 个月，已履行 9 个月后离职，应赔偿多少元？",
        evidence,
        "服务期 24 个月，已履行 9 个月，应赔偿 9999 元。",
    )
    assert patch.verdict.citation_support == 0.0
    assert "number_mismatch" in patch.answer.claims[0].note


def test_derived_date_is_accepted() -> None:
    # 2026-03-01 + 6 个月 = 2026-09-01，再减 30 天 = 2026-08-02。
    evidence = Evidence(
        id="ev1",
        source_name="training_contract",
        title="培训协议",
        content="试用期最长不超过 6 个月；试用期满前 30 天，员工需提交转正申请。",
    )
    patch = _verify(
        "员工 2026 年 3 月 1 日入职，试用期 6 个月，转正申请最晚应在哪一天前提交？",
        evidence,
        "试用期最长不超过 6 个月；员工 2026 年 3 月 1 日入职，试用期满前 30 天，"
        "员工需提交转正申请，最晚应在 2026 年 8 月 2 日前提交。",
    )
    assert patch.verdict.citation_support == 1.0
    assert "date:" in patch.answer.claims[0].note


def test_fabricated_date_still_fails() -> None:
    evidence = Evidence(
        id="ev1",
        source_name="training_contract",
        title="培训协议",
        content="试用期最长不超过 6 个月；试用期满前 30 天，员工需提交转正申请。",
    )
    patch = _verify(
        "员工 2026 年 3 月 1 日入职，试用期 6 个月，转正申请最晚应在哪一天前提交？",
        evidence,
        "试用期最长不超过 6 个月；员工 2026 年 3 月 1 日入职，试用期满前 30 天，"
        "员工需提交转正申请，最晚应在 2026 年 9 月 15 日前提交。",
    )
    assert patch.verdict.citation_support == 0.0
    assert "number_mismatch" in patch.answer.claims[0].note


def test_number_conflict_blocks_passed_even_if_threshold_met() -> None:
    # 3 个 claim 中 2 个完全支持（citation_support=0.667 ≥ 0.6），
    # 但第 3 个文本高度重合、仅数字冲突；数值不一致是硬错误，不能判定通过。
    evidence = Evidence(
        id="ev1",
        source_name="training_contract",
        title="培训协议",
        content="培训费用为 6000 元，服务期 24 个月；已履行 9 个月，未履行部分按比例赔偿。",
    )
    claims = [
        Claim(id="c0", text="培训费用为 6000 元。", evidence_ids=["ev1"]),
        Claim(id="c1", text="服务期为 24 个月。", evidence_ids=["ev1"]),
        Claim(
            id="c2",
            text="培训费用 6000 元，服务期 24 个月，已履行 9 个月，未履行部分按比例赔偿 4000 元。",
            evidence_ids=["ev1"],
        ),
    ]
    state = make_state("培训费 6000 元、服务期 24 个月，已履行 9 个月，应赔偿多少元？").model_copy(
        update={
            "evidence": [evidence],
            "answer": Answer(
                text="培训费用为 6000 元，服务期 24 个月，未履行部分按比例赔偿 4000 元。",
                claims=claims,
            ),
        }
    )
    patch = asyncio.run(verify_node(state, None))
    assert patch.verdict.citation_support == pytest.approx(2 / 3, abs=0.01)
    assert patch.verdict.passed is False
    assert "number_mismatch" in patch.verdict.issues


def test_date_day_component_is_not_treated_as_duration() -> None:
    # "8 月 31 日" 里的 "31 日" 是日期分量，不是 31 天期限；
    # 若误当期限，2026-03-01 减 1 日再加 6 个月会凭空"推出" 2026-08-31。
    evidence = Evidence(
        id="ev1",
        source_name="training_contract",
        title="培训协议",
        content="试用期最长不超过 6 个月；试用期满前 30 天，员工需提交转正申请。",
    )
    patch = _verify(
        "员工 2026 年 3 月 1 日入职，试用期 6 个月，转正申请最晚应在哪一天前提交？",
        evidence,
        "试用期最长不超过 6 个月；员工 2026 年 3 月 1 日入职，试用期满前 30 天，"
        "员工需提交转正申请，最晚应在 2026 年 8 月 31 日前提交。",
    )
    assert patch.verdict.citation_support == 0.0
    assert "number_mismatch" in patch.answer.claims[0].note


def test_weak_overlap_number_conflict_is_classified_as_evidence_gap() -> None:
    # 文本本身几乎不被证据覆盖时，数字不一致只是附带噪声，应归为证据不足，
    # 不能被方案 A 误判为"数值无法确认"。
    evidence = Evidence(
        id="ev1",
        source_name="travel_policy",
        title="差旅与报销管理办法",
        content="一线城市住宿标准为 800 元/晚，出差结束后 30 天内提交报销。",
    )
    patch = _verify(
        "上海办公室停车费标准是多少？",
        evidence,
        "上海办公室停车月租为 999 元。",
    )
    note = patch.answer.claims[0].note
    assert "weak_overlap_with_number_conflict" in note
    assert "number_mismatch" not in note
    assert "number_mismatch" not in patch.verdict.issues
