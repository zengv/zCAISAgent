from __future__ import annotations

import re
from typing import Any

from zcaisagent.domain.models import RunState

_PUNCT = re.compile(r"[\s，。、；：！？,.;:!?¥￥()（）\"'“”‘’]")

# FR-EVAL-01 的 100 条绝对配比；每条样本以 category 归入唯一类别。
TARGET_COVERAGE: dict[str, int] = {
    "single_hop": 30,
    "multi_hop": 25,
    "freshness": 15,
    "no_answer": 15,
    "authz": 5,
    "injection": 10,
}
_CATEGORY_TAGS: dict[str, set[str]] = {
    "single_hop": {"single_hop"},
    "multi_hop": {"multi_hop", "comparison"},
    "freshness": {"time", "freshness", "realtime"},
    "no_answer": {"no_answer"},
    "authz": {"authz", "cross_tenant"},
    "injection": {"injection", "adversarial"},
}
_TARGET_TOTAL = 100


def _category(item: dict[str, Any]) -> str | None:
    category = item.get("category")
    if category in TARGET_COVERAGE:
        return str(category)
    # 兼容旧数据：无 category 时按标签归类。
    tags = {str(tag) for tag in (item.get("tags") or [])}
    for name, aliases in _CATEGORY_TAGS.items():
        if tags & aliases:
            return name
    return None


def _normalize(text: str) -> str:
    return _PUNCT.sub("", text).lower()


def evaluate_case(
    case: dict[str, Any], state: RunState, actions: int | None = None
) -> dict[str, Any]:
    answer_text = state.answer.text if state.answer else ""
    answerable = bool(case.get("answerable", True))
    # 多跳/组合题可用 expected_answers 要求所有要点同时出现，
    # 仍保持子串匹配口径，不引入模型 judge。
    expected_all = [
        _normalize(str(item)) for item in (case.get("expected_answers") or [])
    ]
    expected = _normalize(str(case.get("expected_answer") or ""))
    if answerable:
        if expected_all:
            correct = all(item and item in _normalize(answer_text) for item in expected_all)
        else:
            correct = bool(expected) and expected in _normalize(answer_text)
    else:
        correct = state.status.value == "abstained"

    expected_sources = set(case.get("expected_sources") or [])
    hit_sources = {
        str(item.metadata.get("doc_id") or "")
        for item in state.evidence
        if item.metadata
    }
    recall = (
        len(expected_sources & hit_sources) / len(expected_sources)
        if expected_sources
        else None
    )
    verdict = state.verdict
    return {
        "id": case.get("id"),
        "level": case.get("level", "L1"),
        "category": case.get("category"),
        "tags": list(case.get("tags") or []),
        "scenario": case.get("scenario"),
        "answerable": answerable,
        "status": state.status.value,
        "correct": correct,
        "citation_support": verdict.citation_support if verdict else None,
        "faithfulness": verdict.faithfulness if verdict else None,
        "context_recall": recall,
        "steps": state.step,
        "actions": actions,
        "cost_usd": round(state.budget.used_cost_usd, 6),
        "abstain_reason": (
            state.analysis.get("abstain_reason")
            if state.status.value == "abstained"
            else None
        ),
    }


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    succeeded = sum(1 for item in results if item["status"] == "succeeded")
    abstained = sum(1 for item in results if item["status"] == "abstained")
    failed = total - succeeded - abstained
    # 拒答/失败没有可被证据支撑的生成答案，且可能残留上一轮 verdict，
    # 因此这两个指标只统计 succeeded 样本，避免用拒答文案稀释或虚高分数。
    citations = [
        item["citation_support"]
        for item in results
        if item["status"] == "succeeded" and item["citation_support"] is not None
    ]
    faithfulness = [
        item["faithfulness"]
        for item in results
        if item["status"] == "succeeded" and item["faithfulness"] is not None
    ]
    recalls = [item["context_recall"] for item in results if item["context_recall"] is not None]
    actions = [
        item["actions"] for item in results if item.get("actions") is not None
    ]
    # 区分"因校验失败而拒答"与"证据不足拒答"，避免混为一谈。
    abstain_reasons: dict[str, int] = {}
    for item in results:
        if item["status"] != "abstained":
            continue
        reason = str(item.get("abstain_reason") or "unspecified")
        abstain_reasons[reason] = abstain_reasons.get(reason, 0) + 1
    return {
        "total": total,
        "succeeded": succeeded,
        "abstained": abstained,
        "failed": failed,
        "abstain_reasons": abstain_reasons,
        "success_rate": round(succeeded / total, 3) if total else 0.0,
        "correct_rate": round(sum(1 for item in results if item["correct"]) / total, 3)
        if total
        else 0.0,
        "abstain_accuracy": round(
            sum(
                1
                for item in results
                if (item["status"] == "abstained") != item["answerable"]
            )
            / total,
            3,
        )
        if total
        else 0.0,
        "avg_citation_support": _mean(citations),
        "avg_faithfulness": _mean(faithfulness),
        "avg_context_recall": _mean(recalls),
        "avg_steps": round(sum(item["steps"] for item in results) / total, 2) if total else 0.0,
        "avg_actions": _mean(actions),
        "total_cost_usd": round(sum(item["cost_usd"] for item in results), 6),
    }


def _slices(results: list[dict[str, Any]], key: str) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in results:
        value = item.get(key)
        if isinstance(value, list):
            for entry in value:
                grouped.setdefault(str(entry), []).append(item)
        else:
            grouped.setdefault(str(value), []).append(item)
    return {name: _aggregate(items) for name, items in sorted(grouped.items())}


def coverage_report(results: list[dict[str, Any]]) -> dict[str, Any]:
    """按 FR-EVAL-01 的 100 条绝对配比检查各类样本数与缺口。"""
    total = len(results)
    buckets: dict[str, Any] = {}
    for name, target in TARGET_COVERAGE.items():
        count = sum(1 for item in results if _category(item) == name)
        buckets[name] = {
            "count": count,
            "target": target,
            "ratio": round(count / total, 3) if total else 0.0,
            "target_ratio": target / _TARGET_TOTAL,
            "gap": max(0, target - count),
            "ok": count >= target,
        }
    return {"total": total, "target_total": _TARGET_TOTAL, "buckets": buckets}


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    report = _aggregate(results)
    report["by_level"] = _slices(results, "level")
    report["by_tag"] = _slices(results, "tags")
    report["by_scenario"] = _slices(results, "scenario")
    report["coverage"] = coverage_report(results)
    return report
