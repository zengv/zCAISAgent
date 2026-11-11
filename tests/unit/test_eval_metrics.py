from tests.conftest import make_state
from zcaisagent.domain.enums import RunStatus
from zcaisagent.domain.models import Answer
from zcaisagent.evaluation.metrics import coverage_report, evaluate_case, summarize
from zcaisagent.evaluation.runner import failed_samples, filter_cases


def test_coverage_reports_absolute_target_gaps() -> None:
    results = [
        {"category": "single_hop"},
        {"category": "single_hop"},
        {"category": "no_answer"},
        {"category": "injection"},
        {"category": "multi_hop"},
    ]
    report = coverage_report(results)
    buckets = report["buckets"]
    assert report["total"] == 5
    assert report["target_total"] == 100
    assert buckets["single_hop"]["count"] == 2
    assert buckets["single_hop"]["gap"] == 28
    assert buckets["multi_hop"]["gap"] == 24
    assert buckets["authz"]["ok"] is False


def test_category_takes_precedence_over_tags() -> None:
    report = coverage_report([{"category": "freshness", "tags": ["single_hop"]}])
    assert report["buckets"]["freshness"]["count"] == 1
    assert report["buckets"]["single_hop"]["count"] == 0


def test_empty_dataset_coverage_gap_equals_target() -> None:
    report = coverage_report([])
    assert report["total"] == 0
    assert all(
        bucket["gap"] == bucket["target"] for bucket in report["buckets"].values()
    )


def test_summarize_contains_layered_and_coverage_sections() -> None:
    results = [
        {
            "id": "q1", "level": "L1", "category": "single_hop",
            "tags": ["single_hop"], "scenario": "travel",
            "answerable": True, "status": "succeeded", "correct": True,
            "citation_support": 0.8, "faithfulness": 0.8, "context_recall": 1.0,
            "steps": 5, "cost_usd": 0.001,
        },
        {
            "id": "q2", "level": "L1", "category": "no_answer",
            "tags": ["no_answer"], "scenario": "out_of_scope",
            "answerable": False, "status": "abstained", "correct": True,
            "citation_support": None, "faithfulness": None, "context_recall": None,
            "steps": 6, "cost_usd": 0.0,
        },
    ]
    report = summarize(results)
    assert report["by_level"]["L1"]["total"] == 2
    assert report["by_tag"]["single_hop"]["total"] == 1
    assert report["coverage"]["buckets"]["no_answer"]["count"] == 1


def test_filter_cases_by_tag_and_level() -> None:
    cases = [
        {"id": "q1", "level": "L1", "tags": ["single_hop"]},
        {"id": "q2", "level": "L2", "tags": ["multi_hop"]},
        {"id": "q3", "level": "L1", "tags": ["injection"]},
    ]
    assert [case["id"] for case in filter_cases(cases, tag="injection")] == ["q3"]
    assert [case["id"] for case in filter_cases(cases, level="L1")] == ["q1", "q3"]
    assert [
        case["id"] for case in filter_cases(cases, tag="single_hop", level="L2")
    ] == []


def test_abstained_case_without_answer_is_correct() -> None:
    state = make_state("公司的股票期权计划如何分配？").model_copy(
        update={"status": RunStatus.ABSTAINED}
    )
    item = evaluate_case(
        {"id": "q1", "answerable": False, "tags": ["no_answer"]}, state
    )
    assert item["correct"] is True
    assert item["status"] == "abstained"


def test_expected_answers_requires_all_points() -> None:
    case = {
        "id": "q1",
        "answerable": True,
        "expected_answers": ["500 元", "6 个月"],
        "tags": ["multi_hop"],
    }
    partial = make_state("补贴和调休？").model_copy(
        update={"answer": Answer(text="差旅补贴共 500 元。")}
    )
    complete = make_state("补贴和调休？").model_copy(
        update={"answer": Answer(text="差旅补贴共 500 元，调休需在 6 个月内使用。")}
    )
    assert evaluate_case(case, partial)["correct"] is False
    assert evaluate_case(case, complete)["correct"] is True


def test_citation_and_faithfulness_exclude_abstained() -> None:
    # 拒答样本没有可被证据支撑的答案，残留的 verdict 不应计入这两个指标。
    base = {
        "level": "L1", "category": "single_hop", "tags": [], "scenario": "travel",
        "answerable": True, "correct": True, "steps": 7, "actions": 1,
        "context_recall": None, "cost_usd": 0.0,
    }
    results = [
        {**base, "id": "q1", "status": "succeeded",
         "citation_support": 0.9, "faithfulness": 0.6},
        {**base, "id": "q2", "status": "abstained",
         "citation_support": 0.0, "faithfulness": 0.875},
        {**base, "id": "q3", "status": "failed",
         "citation_support": None, "faithfulness": None},
    ]
    report = summarize(results)
    assert report["avg_citation_support"] == 0.9
    assert report["avg_faithfulness"] == 0.6


def test_avg_actions_is_reported_separately_from_node_steps() -> None:
    # 有效动作次数是效率指标，节点步数继续作为预算口径单独保留。
    results = [
        {
            "id": "q1", "level": "L1", "status": "succeeded", "correct": True,
            "answerable": True, "tags": [], "steps": 7, "actions": 1,
            "citation_support": None, "faithfulness": None, "context_recall": None,
            "cost_usd": 0.0,
        },
        {
            "id": "q2", "level": "L1", "status": "succeeded", "correct": True,
            "answerable": True, "tags": [], "steps": 7, "actions": 2,
            "citation_support": None, "faithfulness": None, "context_recall": None,
            "cost_usd": 0.0,
        },
    ]
    report = summarize(results)
    assert report["avg_steps"] == 7.0
    assert report["avg_actions"] == 1.5


def test_failed_samples_lists_wrong_and_failed_cases() -> None:
    cases = [
        {"id": "q1", "question": "正确题"},
        {"id": "q2", "question": "答错题"},
        {"id": "q3", "question": "执行失败题"},
    ]
    results = [
        {"status": "succeeded", "correct": True, "answerable": True, "steps": 3},
        {"status": "succeeded", "correct": False, "answerable": True, "steps": 4},
        {"status": "failed", "correct": False, "answerable": True, "steps": 1},
    ]
    samples = failed_samples(cases, results)

    assert [item["id"] for item in samples] == ["q2", "q3"]
    assert samples[0]["question"] == "答错题"
    assert samples[1]["status"] == "failed"
