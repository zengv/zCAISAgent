from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zcaisagent.domain.models import Evidence
from zcaisagent.governance.redaction import redact_pii
from zcaisagent.ports.security import AuthContext, GuardrailPort


@dataclass(frozen=True)
class SecurityCase:
    id: str
    category: str
    target: str
    text: str
    should_block: bool
    note: str = ""


def load_security_cases(path: Path) -> list[SecurityCase]:
    cases: list[SecurityCase] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        cases.append(
            SecurityCase(
                id=str(data["id"]),
                category=str(data["category"]),
                target=str(data["target"]),
                text=str(data["text"]),
                should_block=bool(data["should_block"]),
                note=str(data.get("note") or ""),
            )
        )
    return cases


def _rate(passed: int, total: int) -> float:
    return round(passed / total, 4) if total else 0.0


async def _is_blocked(
    guardrail: GuardrailPort, case: SecurityCase, auth: AuthContext
) -> bool:
    if case.target == "input":
        decision = await guardrail.check_input(case.text, auth)
        return not decision.allowed
    if case.target == "evidence":
        evidence = Evidence(id=case.id, source_name="eval", content=case.text)
        decision = await guardrail.check_evidence(evidence, auth)
        return not decision.allowed
    if case.target == "output":
        decision = await guardrail.check_output(case.text, auth)
        if not decision.allowed:
            return True
        # 兼容只脱敏不拦截的实现：PII 文本被改写视为已处理。
        return case.category == "pii" and redact_pii(case.text) != case.text
    raise ValueError(f"unknown security target: {case.target}")


async def run_security_eval(
    guardrail: GuardrailPort, cases: list[SecurityCase]
) -> dict[str, Any]:
    """按输入/证据/输出三处执行红队用例，输出分层与整体通过率。"""
    auth = AuthContext(tenant_id="eval", user_id="eval")
    per_category: dict[str, dict[str, int]] = {}
    failures: list[dict[str, Any]] = []
    passed_total = 0

    for case in cases:
        blocked = await _is_blocked(guardrail, case, auth)
        passed = blocked == case.should_block
        stats = per_category.setdefault(case.category, {"cases": 0, "passed": 0})
        stats["cases"] += 1
        if passed:
            stats["passed"] += 1
            passed_total += 1
        else:
            failures.append(
                {
                    "id": case.id,
                    "category": case.category,
                    "target": case.target,
                    "should_block": case.should_block,
                    "blocked": blocked,
                    "note": case.note,
                }
            )

    return {
        "cases": len(cases),
        "passed": passed_total,
        "overall_pass_rate": _rate(passed_total, len(cases)),
        "by_category": {
            name: {
                "cases": stats["cases"],
                "passed": stats["passed"],
                "pass_rate": _rate(stats["passed"], stats["cases"]),
            }
            for name, stats in sorted(per_category.items())
        },
        "failures": failures,
    }
