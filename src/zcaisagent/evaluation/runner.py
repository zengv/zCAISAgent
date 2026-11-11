from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zcaisagent.container import Container, build_context, create_state
from zcaisagent.domain.enums import AutonomyLevel
from zcaisagent.evaluation.metrics import evaluate_case, summarize
from zcaisagent.orchestration.build import build_default_graph
from zcaisagent.orchestration.runtime import Runtime


def load_cases(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _parse_level(value: str | None) -> AutonomyLevel:
    if not value:
        return AutonomyLevel.L1_FAST
    try:
        return AutonomyLevel(value)
    except ValueError:
        return AutonomyLevel.L1_FAST


# 有效动作次数口径：每个 retriever/工具动作执行一次计 1 次，
# 与节点步数（预算硬上限）分开统计。
_ACTION_NODES = frozenset({"retrieve", "act"})


def _action_count(trace: Any, run_id: str) -> int:
    return sum(
        1
        for event in trace.events(run_id)
        if event.type == "step.finished" and event.node in _ACTION_NODES
    )


def filter_cases(
    cases: list[dict[str, Any]], tag: str | None = None, level: str | None = None
) -> list[dict[str, Any]]:
    """按标签或等级筛选，用于单独跑专项集/分层集。"""
    filtered = cases
    if tag:
        filtered = [case for case in filtered if tag in (case.get("tags") or [])]
    if level:
        filtered = [case for case in filtered if case.get("level") == level]
    return filtered


def failed_samples(
    cases: list[dict[str, Any]], results: list[dict[str, Any]], limit: int = 50
) -> list[dict[str, Any]]:
    """失败样本清单：答错的可答题与执行失败项，供报告与 Studio 定位。"""
    samples: list[dict[str, Any]] = []
    for case, result in zip(cases, results, strict=False):
        failed = result.get("status") == "failed" or (
            bool(result.get("answerable", True)) and not result.get("correct", False)
        )
        if not failed:
            continue
        samples.append(
            {
                "id": case.get("id"),
                "question": str(case.get("question") or "")[:160],
                "status": result.get("status"),
                "correct": result.get("correct"),
                "answerable": result.get("answerable"),
                "steps": result.get("steps"),
                "citation_support": result.get("citation_support"),
                "faithfulness": result.get("faithfulness"),
            }
        )
        if len(samples) >= limit:
            break
    return samples


async def run_dataset(
    container: Container, cases: list[dict[str, Any]]
) -> dict[str, Any]:
    auth = await container.identity.authenticate(
        container.settings.api_key, {"tenant_id": container.settings.default_tenant}
    )
    results: list[dict[str, Any]] = []
    for case in cases:
        level = _parse_level(case.get("level"))
        state = create_state(
            container,
            goal=case["question"],
            tenant_id=case.get("tenant") or container.settings.default_tenant,
            level=level,
            auth=auth,
        )
        state = await Runtime().execute(
            build_default_graph(), state, build_context(container, auth)
        )
        results.append(
            evaluate_case(
                case,
                state,
                actions=_action_count(container.trace, state.run_id),
            )
        )
    report = summarize(results)
    report["failed_samples"] = failed_samples(cases, results)
    return report
