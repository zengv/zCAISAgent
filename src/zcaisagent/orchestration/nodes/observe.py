from __future__ import annotations

from zcaisagent.domain.models import Evidence, RunState
from zcaisagent.domain.patch import StatePatch
from zcaisagent.orchestration.runtime import RunContext


async def observe_node(state: RunState, ctx: RunContext) -> StatePatch:
    action = state.analysis.get("action") or {}
    items = action.get("items") or []
    evidence = [Evidence.model_validate(item) for item in items]
    quarantined: list[str] = []

    if ctx.guardrail is not None:
        kept: list[Evidence] = []
        for item in evidence:
            decision = await ctx.guardrail.check_evidence(item, ctx.auth)
            if decision.allowed:
                kept.append(item)
            else:
                quarantined.append(item.id)
        evidence = kept

    evidence = [
        item.model_copy(update={"snippet": item.snippet or item.content[:160]})
        for item in evidence
    ]
    notes = [f"observe:{action.get('step_id', '?')}:{len(evidence)}"]
    if quarantined:
        notes.append("evidence_quarantined:" + ",".join(sorted(quarantined)))

    return StatePatch(
        append_evidence=evidence,
        analysis={
            "observation": {
                "step_id": action.get("step_id"),
                "count": len(evidence),
                "sources": action.get("sources", []),
            }
        },
        append_notes=notes,
    )
