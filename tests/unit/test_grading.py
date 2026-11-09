from zcaisagent.evaluation.grading import summarize_grading


def _item(case_id: str, expected: str, actual: str, basis: str = "rule") -> dict:
    return {
        "id": case_id,
        "question": case_id,
        "expected_level": expected,
        "actual_level": actual,
        "correct": expected == actual,
        "intent": "查事实",
        "complexity": "simple",
        "basis": basis,
        "scenario": "travel",
    }


def test_grading_summary_reports_accuracy_and_confusion() -> None:
    results = [
        _item("g1", "L1", "L1"),
        _item("g2", "L1", "L1"),
        _item("g3", "L2", "L1"),
        _item("g4", "L2", "L2"),
        _item("g5", "L3", "L3"),
        _item("g6", "L3", "L2"),
    ]
    report = summarize_grading(results)
    assert report["total"] == 6
    assert report["accuracy"] == 0.667
    assert report["pass"] is False
    assert report["confusion"]["L2"]["L1"] == 1
    assert report["by_level"]["L1"]["recall"] == 1.0
    assert report["by_level"]["L3"]["recall"] == 0.5
    assert len(report["failures"]) == 2


def test_grading_summary_passes_target_at_85_percent() -> None:
    results = [_item(f"g{i}", "L1", "L1") for i in range(17)]
    results += [_item(f"h{i}", "L2", "L1") for i in range(3)]
    report = summarize_grading(results)
    assert report["accuracy"] == 0.85
    assert report["pass"] is True
