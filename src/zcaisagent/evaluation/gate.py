from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GateThresholds:
    correct_rate: float = 0.0
    citation_support: float = 0.0
    faithfulness: float = 0.0
    context_recall: float = 0.0
    abstain_accuracy: float = 0.0
    allow_failed: int = 0


@dataclass(frozen=True)
class GateResult:
    passed: bool
    violations: list[str]


# 配置键 -> 报告键；阈值统一为下界，allow_failed 为失败样本上限。
_METRICS: dict[str, str] = {
    "correct_rate": "correct_rate",
    "citation_support": "avg_citation_support",
    "faithfulness": "avg_faithfulness",
    "context_recall": "avg_context_recall",
    "abstain_accuracy": "abstain_accuracy",
}


def load_thresholds(path: Path) -> GateThresholds:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return GateThresholds(
        correct_rate=float(data.get("correct_rate", 0.0)),
        citation_support=float(data.get("citation_support", 0.0)),
        faithfulness=float(data.get("faithfulness", 0.0)),
        context_recall=float(data.get("context_recall", 0.0)),
        abstain_accuracy=float(data.get("abstain_accuracy", 0.0)),
        allow_failed=int(data.get("allow_failed", 0)),
    )


def check(report: dict[str, Any], thresholds: GateThresholds) -> GateResult:
    violations: list[str] = []
    for name, key in _METRICS.items():
        threshold = getattr(thresholds, name)
        value = report.get(key)
        if value is None:
            # 阈值未要求时可容忍指标缺失（例如无 succeeded 样本）。
            if threshold > 0:
                violations.append(f"{name}: missing (required >= {threshold})")
            continue
        if float(value) < threshold:
            violations.append(f"{name}: {value} < {threshold}")

    failed = int(report.get("failed", 0))
    if failed > thresholds.allow_failed:
        violations.append(f"failed: {failed} > {thresholds.allow_failed}")

    return GateResult(passed=not violations, violations=violations)
