from __future__ import annotations

from zcaisagent.domain.models import Evidence, RunState
from zcaisagent.domain.patch import StatePatch
from zcaisagent.orchestration.runtime import RunContext
from zcaisagent.ports.security import AuditEvent


async def observe_node(state: RunState, ctx: RunContext) -> StatePatch:
    action = state.analysis.get("action") or {}
    items = action.get("items") or []
    evidence = [Evidence.model_validate(item) for item in items]
    quarantined: list[tuple[Evidence, str | None, str | None]] = []

    if ctx.guardrail is not None:
        kept: list[Evidence] = []
        for item in evidence:
            decision = await ctx.guardrail.check_evidence(item, ctx.auth)
            if decision.allowed:
                kept.append(item)
            else:
                quarantined.append(
                    (item, decision.category, decision.details.get("marker"))
                )
        evidence = kept

    evidence = [
        item.model_copy(update={"snippet": item.snippet or item.content[:160]})
        for item in evidence
    ]
    notes = [f"observe:{action.get('step_id', '?')}:{len(evidence)}"]
    if quarantined:
        notes.append(
            "evidence_quarantined:"
            + ",".join(sorted(item.id for item, _, _ in quarantined))
        )
        if ctx.audit is not None:
            for item, category, marker in quarantined:
                try:
                    await ctx.audit.append(
                        AuditEvent(
                            action="guardrail.evidence_quarantined",
                            actor=ctx.auth.user_id,
                            tenant_id=ctx.auth.tenant_id,
                            run_id=state.run_id,
                            detail={
                                "evidence_id": item.id,
                                "source_name": item.source_name,
                                "category": category,
                                "marker": marker,
                            },
                        )
                    )
                except Exception:  # noqa: BLE001 - 审计失败不阻断 Run
                    pass

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
