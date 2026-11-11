import json
import time

import pytest

from zcaisagent.domain.enums import RunStatus
from zcaisagent.domain.models import Answer, Budget, RunState
from zcaisagent.evaluation.observability import (
    aggregate_runs,
    build_metrics,
    percentile,
    quality_snapshot,
    report_summary,
    window_to_seconds,
)


def _state(
    run_id: str,
    status: RunStatus,
    created_ts: float,
    finished_ts: float = 0.0,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost: float = 0.0,
) -> RunState:
    return RunState(
        run_id=run_id,
        thread_id=f"thread_{run_id}",
        tenant_id="demo",
        user_id="dev",
        goal="q",
        status=status,
        created_ts=created_ts,
        finished_ts=finished_ts,
        budget=Budget(
            used_tokens=tokens_in + tokens_out,
            used_tokens_in=tokens_in,
            used_tokens_out=tokens_out,
            used_cost_usd=cost,
        ),
        answer=Answer(text="a", abstained=status == RunStatus.ABSTAINED),
    )


def test_percentile_linear_interpolation() -> None:
    values = [10, 20, 30, 40]
    assert percentile(values, 0) == 10
    assert percentile(values, 50) == 25
    assert percentile(values, 95) == 38.5
    assert percentile(values, 100) == 40
    assert percentile([5], 95) == 5
    assert percentile([], 50) == 0.0
    assert percentile([40, 10, 30, 20], 50) == 25


def test_window_to_seconds_valid_and_invalid() -> None:
    assert window_to_seconds("24h") == 86400
    assert window_to_seconds("1h") == 3600
    assert window_to_seconds("30m") == 1800
    assert window_to_seconds("7d") == 604800
    for invalid in ("", "h", "24x", "-1h", "0h", "abc"):
        with pytest.raises(ValueError):
            window_to_seconds(invalid)


def test_aggregate_runs_empty() -> None:
    aggregate = aggregate_runs([], now=1000.0, window_s=3600)
    assert aggregate["empty"] is True
    assert aggregate["count"] == 0
    assert aggregate["success_rate"] == 0.0
    assert aggregate["abstain_rate"] == 0.0
    assert aggregate["latency_ms"] == {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    assert aggregate["tokens_total"] == 0
    assert aggregate["cost_usd"] == 0.0


def test_aggregate_runs_counts_and_latency() -> None:
    states = [
        _state(
            "run_a", RunStatus.SUCCEEDED, 1000.0, finished_ts=1002.0,
            tokens_in=100, tokens_out=50, cost=0.002,
        ),
        _state(
            "run_b", RunStatus.ABSTAINED, 1010.0, finished_ts=1011.0,
            tokens_in=10, tokens_out=5, cost=0.001,
        ),
        _state("run_c", RunStatus.FAILED, 1020.0),
    ]
    aggregate = aggregate_runs(states, now=2000.0, window_s=10_000)
    assert aggregate["empty"] is False
    assert aggregate["count"] == 3
    assert aggregate["success_count"] == 1
    assert aggregate["success_rate"] == 0.333
    assert aggregate["abstained"] == 1
    assert aggregate["abstain_rate"] == 0.333
    assert aggregate["latency_ms"]["p50"] == 1500.0
    assert aggregate["latency_ms"]["p95"] == 1950.0
    assert aggregate["latency_ms"]["p99"] == 1990.0
    assert aggregate["latency_ms"]["max"] == 2000.0
    assert aggregate["tokens_total"] == 165
    assert aggregate["tokens_in"] == 110
    assert aggregate["tokens_out"] == 55
    assert aggregate["cached_tokens"] == 0
    assert aggregate["cost_usd"] == pytest.approx(0.003)


def test_aggregate_runs_window_filters_old_runs() -> None:
    states = [
        _state("run_in", RunStatus.SUCCEEDED, 7000.0),
        _state("run_out", RunStatus.SUCCEEDED, 5000.0),
        _state("run_edge", RunStatus.SUCCEEDED, 6400.0),
    ]
    aggregate = aggregate_runs(states, now=10_000.0, window_s=3600)
    assert aggregate["count"] == 2
    assert aggregate["success_count"] == 2


def test_build_metrics_contract() -> None:
    now = time.time()
    states = [
        _state(
            "run_a", RunStatus.SUCCEEDED, now - 1, finished_ts=now,
            tokens_in=20, tokens_out=10, cost=0.01,
        )
    ]
    cache = {"enabled": True, "hits": 3, "misses": 1, "hit_rate": 0.75}
    metrics = build_metrics(states, cache, "1h", {"available": False})
    assert metrics["window"] == "1h"
    assert metrics["empty"] is False
    assert metrics["requests"] == 1
    assert metrics["success_rate"] == 1.0
    assert metrics["abstain_rate"] == 0.0
    assert metrics["latency_ms"]["p50"] == pytest.approx(1000.0, abs=1.0)
    assert metrics["tokens"] == {"total": 30, "in": 20, "out": 10}
    assert metrics["cost_usd"] == 0.01
    assert metrics["cache"] == {
        "enabled": True, "hits": 3, "misses": 1, "hit_rate": 0.75
    }
    assert metrics["quality"]["available"] is False

    with pytest.raises(ValueError):
        build_metrics([], None, "nope", None)


def test_build_metrics_defaults_when_cache_and_quality_missing() -> None:
    metrics = build_metrics([], None, "24h", None)
    assert metrics["empty"] is True
    assert metrics["cache"] == {
        "enabled": False, "hits": 0, "misses": 0, "hit_rate": 0.0
    }
    assert metrics["quality"]["available"] is False


def test_quality_snapshot_missing_files(tmp_path) -> None:
    snapshot = quality_snapshot(tmp_path / "latest.json", tmp_path / "gate.toml")
    assert snapshot == {"available": False}


def test_quality_snapshot_falls_back_to_baseline(tmp_path) -> None:
    baseline_dir = tmp_path / "baselines"
    baseline_dir.mkdir()
    baseline = baseline_dir / "baseline.json"
    baseline.write_text(
        json.dumps({"total": 1, "correct_rate": 1.0}), encoding="utf-8"
    )
    thresholds_path = tmp_path / "gate.toml"
    thresholds_path.write_text("correct_rate = 0.5\n", encoding="utf-8")

    snapshot = quality_snapshot(
        tmp_path / "missing.json", thresholds_path, baseline_dir=baseline_dir
    )

    assert snapshot["available"] is True
    assert snapshot["path"] == str(baseline)
    assert snapshot["gate"] == {"passed": True, "violations": []}


def test_report_summary_latest_keys() -> None:
    summary = report_summary(
        {
            "correct_rate": 0.97,
            "avg_citation_support": 0.98,
            "avg_faithfulness": 0.99,
            "avg_context_recall": 0.9,
            "abstain_accuracy": 0.95,
            "cases": 10,
            "generated_at": "2026-01-01T00:00:00+00:00",
        }
    )
    assert summary == {
        "correctness": 0.97,
        "citation_support": 0.98,
        "faithfulness": 0.99,
        "context_recall": 0.9,
        "abstain_accuracy": 0.95,
        "cases": 10,
        "generated_at": "2026-01-01T00:00:00+00:00",
    }


def test_report_summary_baseline_keys_and_missing() -> None:
    summary = report_summary(
        {
            "correct_rate": 0.38,
            "citation_support": 1.0,
            "faithfulness": 1.0,
            "context_recall": 1.0,
            "total": 100,
        }
    )
    assert summary["correctness"] == 0.38
    assert summary["citation_support"] == 1.0
    assert summary["faithfulness"] == 1.0
    assert summary["context_recall"] == 1.0
    assert summary["abstain_accuracy"] is None
    assert summary["cases"] == 100
    assert summary["generated_at"] is None

    empty = report_summary(None)
    assert set(empty) == {
        "correctness", "citation_support", "faithfulness", "context_recall",
        "abstain_accuracy", "cases", "generated_at",
    }
    assert all(value is None for value in empty.values())


def test_quality_snapshot_merges_report_and_gate(tmp_path) -> None:
    report = {
        "total": 2,
        "failed": 0,
        "correct_rate": 0.95,
        "abstain_accuracy": 0.95,
        "avg_citation_support": 0.9,
        "avg_faithfulness": 0.9,
        "avg_context_recall": 0.9,
        "generated_at": "2026-01-01T00:00:00+00:00",
    }
    report_path = tmp_path / "latest.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    thresholds_path = tmp_path / "gate.toml"
    thresholds_path.write_text(
        "correct_rate = 0.9\n"
        "citation_support = 0.85\n"
        "faithfulness = 0.8\n"
        "context_recall = 0.85\n"
        "abstain_accuracy = 0.9\n"
        "allow_failed = 0\n",
        encoding="utf-8",
    )

    snapshot = quality_snapshot(report_path, thresholds_path)
    assert snapshot["available"] is True
    assert snapshot["gate"] == {"passed": True, "violations": []}
    assert snapshot["thresholds"]["correct_rate"] == 0.9
    assert snapshot["path"] == str(report_path)
    assert snapshot["generated_at"] == "2026-01-01T00:00:00+00:00"

    report["correct_rate"] = 0.5
    report_path.write_text(json.dumps(report), encoding="utf-8")
    failing = quality_snapshot(report_path, thresholds_path)
    assert failing["gate"]["passed"] is False
    assert failing["gate"]["violations"]
