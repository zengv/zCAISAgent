from pathlib import Path

from zcaisagent.evaluation.gate import GateThresholds, check, load_thresholds


def _report(**overrides) -> dict:
    base = {
        "correct_rate": 0.95,
        "avg_citation_support": 0.9,
        "avg_faithfulness": 0.9,
        "avg_context_recall": 0.9,
        "abstain_accuracy": 0.95,
        "failed": 0,
    }
    base.update(overrides)
    return base


def test_passes_when_all_metrics_meet_thresholds() -> None:
    result = check(_report(), GateThresholds(0.9, 0.85, 0.8, 0.85, 0.9, 0))
    assert result.passed
    assert result.violations == []


def test_lists_violations_below_threshold() -> None:
    result = check(_report(correct_rate=0.5), GateThresholds(correct_rate=0.9))
    assert not result.passed
    assert any("correct_rate" in item for item in result.violations)


def test_failed_cases_block_gate() -> None:
    result = check(_report(failed=1), GateThresholds(allow_failed=0))
    assert not result.passed
    assert any("failed" in item for item in result.violations)


def test_load_thresholds_from_toml(tmp_path: Path) -> None:
    path = tmp_path / "gate.toml"
    path.write_text("correct_rate = 0.9\nallow_failed = 2\n", encoding="utf-8")
    thresholds = load_thresholds(path)
    assert thresholds.correct_rate == 0.9
    assert thresholds.allow_failed == 2
