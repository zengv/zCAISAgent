from zcaisagent.domain.models import RunState
from zcaisagent.domain.patch import StatePatch
from zcaisagent.orchestration.policies.evidence import grade_evidence, judge_sufficiency
from zcaisagent.orchestration.runtime import RunContext


async def grade_node(state: RunState, ctx: RunContext) -> StatePatch:
    # retrieve 节点的 rerank 分存在时优先作为相关性；否则走词法相关性逻辑。
    rerank_scores = state.analysis.get("rerank_scores") or None
    grades = grade_evidence(state.goal, state.evidence, rerank_scores=rerank_scores)
    sufficiency = judge_sufficiency(state.goal, state.evidence, grades)
    by_id = {grade.evidence_id: grade for grade in grades}
    updated = [
        item.model_copy(
            update={
                "freshness": by_id[item.id].freshness,
                "consistency": by_id[item.id].consistency,
                "final_score": by_id[item.id].final,
                "grade": by_id[item.id].decision,
            }
        )
        for item in state.evidence
    ]
    kept = sum(1 for grade in grades if grade.decision != "drop")
    # 修复补检索后仍不充分且证据数未增，判定无进展并直接拒答，
    # 不再进入第二轮 repair（其结果必然是 abstain）。
    baseline = state.analysis.get("evidence_baseline")
    no_progress = (
        sufficiency.decision == "insufficient"
        and baseline is not None
        and len(updated) <= int(baseline)
    )
    notes = [
        f"graded:keep={kept}/total={len(grades)}",
        f"sufficiency:{sufficiency.decision}:coverage={sufficiency.coverage}",
    ]
    if no_progress:
        notes.append(f"no_progress:evidence={len(updated)}<=baseline={baseline}")
    return StatePatch(
        replace_evidence=updated,
        analysis={
            "sufficiency": sufficiency.model_dump(),
            "evidence_count": len(updated),
            "no_progress": no_progress,
        },
        append_notes=notes,
    )
