"""运营指标聚合：延迟分位、成功率、成本与质量门禁快照。"""

from __future__ import annotations

import math
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zcaisagent.domain.enums import RunStatus
from zcaisagent.domain.models import RunState
from zcaisagent.evaluation.gate import GateThresholds, check, load_thresholds
from zcaisagent.evaluation.report import load_report, resolve_report_path

_WINDOW_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def percentile(values: list[float], p: float) -> float:
    """线性插值分位数；空序列返回 0.0。"""
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if p <= 0:
        return ordered[0]
    if p >= 100:
        return ordered[-1]
    rank = (len(ordered) - 1) * p / 100
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def window_to_seconds(window: str) -> int:
    """把 24h / 30m / 7d 这类窗口转换为秒；非法值抛 ValueError。"""
    text = (window or "").strip().lower()
    if len(text) < 2 or text[-1] not in _WINDOW_UNITS:
        raise ValueError(f"invalid window: {window}")
    try:
        amount = float(text[:-1])
    except ValueError:
        raise ValueError(f"invalid window: {window}") from None
    if amount <= 0:
        raise ValueError(f"invalid window: {window}")
    return int(amount * _WINDOW_UNITS[text[-1]])


def _latency_ms(state: RunState) -> float | None:
    if state.created_ts <= 0 or state.finished_ts <= 0:
        return None
    return max(0.0, (state.finished_ts - state.created_ts) * 1000)


def aggregate_runs(
    states: list[RunState],
    now: float | None = None,
    window_s: int | None = None,
) -> dict[str, Any]:
    """聚合窗口内的 Run：请求量、成功率、延迟分位、tokens 与成本。"""
    if window_s is not None:
        cutoff = (now if now is not None else time.time()) - window_s
        states = [state for state in states if state.created_ts >= cutoff]

    count = len(states)
    success_count = sum(
        1 for state in states if state.status == RunStatus.SUCCEEDED
    )
    abstained = sum(
        1 for state in states if state.status == RunStatus.ABSTAINED
    )
    latencies = [
        latency for state in states if (latency := _latency_ms(state)) is not None
    ]
    return {
        "count": count,
        "success_count": success_count,
        "success_rate": round(success_count / count, 3) if count else 0.0,
        "abstained": abstained,
        "abstain_rate": round(abstained / count, 3) if count else 0.0,
        "latency_ms": {
            "p50": round(percentile(latencies, 50), 2),
            "p95": round(percentile(latencies, 95), 2),
            "p99": round(percentile(latencies, 99), 2),
            "max": round(max(latencies), 2) if latencies else 0.0,
        },
        "tokens_total": sum(state.budget.used_tokens for state in states),
        "tokens_in": sum(state.budget.used_tokens_in for state in states),
        "tokens_out": sum(state.budget.used_tokens_out for state in states),
        "cached_tokens": sum(state.budget.used_cached_tokens for state in states),
        "cost_usd": round(sum(state.budget.used_cost_usd for state in states), 6),
        "empty": count == 0,
    }


def _thresholds_dict(thresholds: GateThresholds) -> dict[str, Any]:
    return asdict(thresholds)


def quality_snapshot(
    report_path: Path,
    thresholds_path: Path | None = None,
    baseline_dir: Path | None = None,
    gate_path: Path | None = None,
) -> dict[str, Any]:
    """读取评估报告并计算门禁结果；报告或阈值缺失时降级为不可用。

    report_path 缺失时回退 baseline_dir 下最新的 *.json；gate_path 优先于
    thresholds_path，二者都缺省时使用 configs/eval_gate.toml。
    """
    resolved = resolve_report_path(Path(report_path), baseline_dir)
    if resolved is None:
        return {"available": False}
    report = load_report(resolved)
    if report is None:
        return {"available": False}
    thresholds_file = gate_path or thresholds_path or Path("configs/eval_gate.toml")
    try:
        thresholds = load_thresholds(Path(thresholds_file))
    except (OSError, ValueError):
        return {"available": False}

    result = check(report, thresholds)
    generated_at = report.get("generated_at")
    if not generated_at:
        generated_at = datetime.fromtimestamp(resolved.stat().st_mtime, UTC).isoformat()
    return {
        "available": True,
        "path": str(resolved),
        "generated_at": generated_at,
        "report": report,
        "thresholds": _thresholds_dict(thresholds),
        "gate": {"passed": result.passed, "violations": result.violations},
    }


def report_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    """提取看板需要的质量指标；兼容 latest（cases）与 baseline（total）键名。"""
    data = report or {}
    return {
        "correctness": _number(data, "correct_rate", "correctness"),
        "citation_support": _number(data, "avg_citation_support", "citation_support"),
        "faithfulness": _number(data, "avg_faithfulness", "faithfulness"),
        "context_recall": _number(data, "avg_context_recall", "context_recall"),
        "abstain_accuracy": _number(data, "abstain_accuracy"),
        "cases": _count(data, "cases", "total"),
        "generated_at": data.get("generated_at"),
    }


def _number(data: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _count(data: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, int):
            return value
    return None


def build_metrics(
    states: list[RunState],
    cache_stats: dict[str, Any] | None,
    window: str,
    quality: dict[str, Any] | None,
) -> dict[str, Any]:
    """把 Run 聚合、缓存统计与质量门禁合并为看板契约。"""
    aggregate = aggregate_runs(states, window_s=window_to_seconds(window))
    cache = cache_stats or {}
    quality = quality or {"available": False}
    return {
        "window": window,
        "empty": aggregate["empty"],
        "requests": aggregate["count"],
        "success_rate": aggregate["success_rate"],
        "abstain_rate": aggregate["abstain_rate"],
        "latency_ms": aggregate["latency_ms"],
        "tokens": {
            "total": aggregate["tokens_total"],
            "in": aggregate["tokens_in"],
            "out": aggregate["tokens_out"],
        },
        "cost_usd": aggregate["cost_usd"],
        "cache": {
            "enabled": bool(cache.get("enabled", False)),
            "hits": int(cache.get("hits", 0)),
            "misses": int(cache.get("misses", 0)),
            "hit_rate": float(cache.get("hit_rate", 0.0)),
        },
        "quality": {
            "available": bool(quality.get("available", False)),
            "generated_at": quality.get("generated_at"),
            "path": quality.get("path", ""),
            "gate": quality.get("gate", {}),
            "report": quality.get("report", {}),
        },
    }
