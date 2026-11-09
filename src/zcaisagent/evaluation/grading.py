from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zcaisagent.container import Container, build_context, create_state
from zcaisagent.orchestration.nodes.analyze import analyze_node


def load_grading_cases(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _empty_confusion(labels: list[str]) -> dict[str, dict[str, int]]:
    return {label: {actual: 0 for actual in labels} for label in labels}


def summarize_grading(results: list[dict[str, Any]]) -> dict[str, Any]:
    """按期望等级与实际等级计算准确率、混淆矩阵与各等级召回。"""
    total = len(results)
    correct = sum(1 for item in results if item["correct"])
    labels = sorted({item["expected_level"] for item in results})
    confusion = _empty_confusion(labels)
    for item in results:
        expected = item["expected_level"]
        actual = item["actual_level"]
        confusion.setdefault(expected, {})
        confusion[expected][actual] = confusion[expected].get(actual, 0) + 1
    per_level: dict[str, Any] = {}
    for label in labels:
        cases = [item for item in results if item["expected_level"] == label]
        hit = sum(1 for item in cases if item["correct"])
        per_level[label] = {
            "total": len(cases),
            "correct": hit,
            "recall": round(hit / len(cases), 3) if cases else 0.0,
        }
    basis: dict[str, int] = {}
    for item in results:
        key = str(item.get("basis") or "unknown")
        basis[key] = basis.get(key, 0) + 1
    return {
        "total": total,
        "correct": correct,
        "accuracy": round(correct / total, 3) if total else 0.0,
        "target": 0.85,
        "pass": bool(total) and correct / total >= 0.85,
        "confusion": confusion,
        "by_level": per_level,
        "basis": basis,
        "failures": [item for item in results if not item["correct"]],
    }


async def run_grading(
    container: Container, cases: list[dict[str, Any]]
) -> dict[str, Any]:
    """不强制等级，直接测量 analyze 的分级结果；route 节点按该结果透传。"""
    auth = await container.identity.authenticate(
        container.settings.api_key, {"tenant_id": container.settings.default_tenant}
    )
    ctx = build_context(container, auth)
    results: list[dict[str, Any]] = []
    for case in cases:
        state = create_state(
            container,
            goal=case["question"],
            tenant_id=case.get("tenant") or container.settings.default_tenant,
            level=None,
            auth=auth,
        )
        patch = await analyze_node(state, ctx)
        level = patch.level or state.level
        analysis = patch.analysis or {}
        results.append(
            {
                "id": case.get("id"),
                "question": case["question"],
                "expected_level": case["expected_level"],
                "actual_level": level.value,
                "correct": level.value == case["expected_level"],
                "intent": analysis.get("intent"),
                "complexity": analysis.get("complexity"),
                "basis": analysis.get("basis"),
                "scenario": case.get("scenario"),
            }
        )
    return summarize_grading(results)
