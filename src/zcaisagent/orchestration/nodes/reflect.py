from __future__ import annotations

from zcaisagent.domain.models import RunState
from zcaisagent.domain.patch import StatePatch
from zcaisagent.orchestration.runtime import RunContext


async def reflect_node(state: RunState, ctx: RunContext) -> StatePatch:
    plan = state.plan
    total = len(plan.steps) if plan else 0
    pending = (
        sum(1 for step in plan.steps if step.status == "pending") if plan else 0
    )
    done = total - pending
    decision = "complete" if pending == 0 else "continue"
    return StatePatch(
        analysis={
            "loop": {
                "decision": decision,
                "total": total,
                "done": done,
                "pending": pending,
                "evidence": len(state.evidence),
            }
        },
        append_notes=[f"reflect:{decision}:{done}/{total}"],
    )
