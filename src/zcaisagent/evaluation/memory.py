from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zcaisagent.capabilities.memory.policies import write_explicit_memory
from zcaisagent.container import Container, build_context, create_state
from zcaisagent.domain.models import Message
from zcaisagent.orchestration.build import build_default_graph
from zcaisagent.orchestration.runtime import Runtime

_MEMORY_INJECTED_PREFIX = "memory:injected:"


def load_memory_cases(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _rate(hits: int, total: int) -> float:
    return round(hits / total, 4) if total else 0.0


async def run_memory_eval(
    container: Container, cases: list[dict[str, Any]]
) -> dict[str, Any]:
    """顺序执行多轮对话，统计偏好召回率与合成注入率。"""
    failures: list[dict[str, Any]] = []
    checks = 0
    recall_hits = 0
    injection_hits = 0

    for case in cases:
        tenant_id = case.get("tenant") or container.settings.default_tenant
        user_id = case.get("user") or f"eval_{case['id']}"
        expected = str(case.get("expected_preference_keyword") or "")
        auth = await container.identity.authenticate(
            container.settings.api_key,
            {"tenant_id": tenant_id, "user_id": user_id},
        )
        history: list[Message] = []
        for turn, goal in enumerate(case.get("goals") or []):
            state = create_state(
                container,
                goal=goal,
                tenant_id=tenant_id,
                user_id=user_id,
                auth=auth,
                history=history or None,
            )
            ctx = build_context(container, auth)
            await write_explicit_memory(container.memory, goal, ctx)
            result = await Runtime().execute(build_default_graph(), state, ctx)
            history = list(result.messages)
            if turn == 0:
                continue

            checks += 1
            preferences = (result.analysis.get("memory") or {}).get("preferences") or []
            recalled = any(expected in str(item) for item in preferences)
            injected = any(
                note.startswith(_MEMORY_INJECTED_PREFIX) for note in result.working_notes
            )
            recall_hits += int(recalled)
            injection_hits += int(injected)
            if not recalled:
                failures.append(
                    {
                        "id": case["id"],
                        "turn": turn,
                        "reason": "recall_miss",
                        "expected": expected,
                        "preferences": preferences,
                    }
                )
            if not injected:
                failures.append(
                    {
                        "id": case["id"],
                        "turn": turn,
                        "reason": "injection_miss",
                        "status": result.status.value,
                    }
                )

    return {
        "cases": len(cases),
        "recall_rate": _rate(recall_hits, checks),
        "injection_rate": _rate(injection_hits, checks),
        "failures": failures,
    }
