from datetime import UTC, datetime, timedelta

from zcaisagent.domain.models import Evidence
from zcaisagent.orchestration.policies.evidence import grade_evidence, judge_sufficiency


def _evidence(
    evidence_id: str,
    content: str,
    relevance: float = 0.8,
    authority: str = "primary",
    published_at: datetime | None = None,
) -> Evidence:
    return Evidence(
        id=evidence_id,
        source_name=evidence_id.split("#")[0],
        title="测试文档",
        content=content,
        relevance=relevance,
        authority=authority,
        published_at=published_at,
    )


def test_grade_uses_four_dimension_weights() -> None:
    now = datetime(2026, 11, 8, tzinfo=UTC)
    item = _evidence("doc#0", "一线城市住宿标准为 800 元/晚", published_at=now)
    grade = grade_evidence("一线城市住宿标准是多少？", [item], now=now)[0]
    assert grade.relevance > 0
    assert grade.authority == 1.0
    assert grade.freshness == 1.0
    assert grade.decision == "keep"
    expected = (
        grade.relevance * 0.45 + 1.0 * 0.25 + 1.0 * 0.15 + grade.consistency * 0.15
    )
    assert abs(grade.final - round(expected, 4)) < 1e-4


def test_stale_evidence_loses_freshness_score() -> None:
    now = datetime(2026, 11, 8, tzinfo=UTC)
    item = _evidence(
        "doc#0",
        "一线城市住宿标准为 800 元/晚",
        published_at=now - timedelta(days=730),
    )
    grade = grade_evidence("一线城市住宿标准是多少？", [item], now=now)[0]
    assert grade.freshness < 0.3


def test_unrelated_low_score_evidence_is_dropped() -> None:
    question = "一线城市住宿标准是多少？"
    relevant = _evidence("doc#0", "一线城市住宿标准为 800 元/晚")
    unrelated = _evidence("other#0", "公司年会安排在十二月", relevance=0.0, authority="unknown")
    grades = grade_evidence(question, [relevant, unrelated])
    by_id = {grade.evidence_id: grade for grade in grades}
    assert by_id["other#0"].decision == "drop"
    assert by_id["doc#0"].decision == "keep"


def test_conflicting_evidence_is_kept_side_by_side() -> None:
    question = "一线城市住宿标准是多少？"
    left = _evidence("doc#0", "一线城市住宿标准为 800 元/晚，需提前申请。")
    right = _evidence("doc#1", "一线城市住宿标准为 500 元/晚，需提前申请。")
    grades = grade_evidence(question, [left, right])
    assert all(grade.decision == "conflict" for grade in grades)
    sufficiency = judge_sufficiency(question, [left, right], grades)
    assert sufficiency.decision == "conflict"
    assert sufficiency.coverage >= 0.7


def test_sufficiency_insufficient_without_qualifying_evidence() -> None:
    question = "一线城市住宿标准是多少？"
    item = _evidence("other#0", "公司年会安排在十二月", relevance=0.0, authority="unknown")
    grades = grade_evidence(question, [item])
    sufficiency = judge_sufficiency(question, [item], grades)
    assert sufficiency.decision == "insufficient"
    assert sufficiency.missing_aspects


def test_sufficiency_marks_qualifying_evidence_as_sufficient() -> None:
    question = "一线城市住宿标准是多少？"
    item = _evidence("doc#0", "一线城市住宿标准为 800 元/晚")
    grades = grade_evidence(question, [item])
    sufficiency = judge_sufficiency(question, [item], grades)
    assert sufficiency.decision == "sufficient"
    assert sufficiency.coverage == 1.0


def test_sufficiency_rejects_evidence_without_topic_coverage() -> None:
    # 字面重合（“公司/提供/补贴”等通用词）不能证明证据覆盖了所问主题。
    question = "公司是否提供员工食堂补贴？"
    item = _evidence("hr_leave#0", "员工累计工作满 1 年不满 10 年的，年假 5 天。")
    grades = grade_evidence(question, [item])
    sufficiency = judge_sufficiency(question, [item], grades)
    assert sufficiency.decision == "insufficient"


def test_sufficiency_accepts_matched_topic_even_with_filler_words() -> None:
    question = "年假可以延到什么时候？"
    item = _evidence("hr_leave#0", "年假原则上当年使用，经审批可延至次年 3 月底。")
    grades = grade_evidence(question, [item])
    sufficiency = judge_sufficiency(question, [item], grades)
    assert sufficiency.decision == "sufficient"


def test_short_question_is_not_diluted_by_question_filler_words() -> None:
    # "有什么规定"等疑问虚词不应占用相关性分母，否则命中主题的证据会卡在门槛下。
    question = "调休有什么规定？"
    item = _evidence(
        "attendance_overtime#1",
        "加班可申请调休。调休需在加班发生后 6 个月内使用，逾期作废，不予展期。",
        relevance=0.2308,
        authority="secondary",
    )
    grades = grade_evidence(question, [item])
    assert grades[0].relevance > 0.2308
    sufficiency = judge_sufficiency(question, [item], grades)
    assert sufficiency.decision == "sufficient"


