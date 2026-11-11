from zcaisagent.domain.models import RunState
from zcaisagent.domain.patch import StatePatch


def apply_patch(state: RunState, patch: StatePatch) -> RunState:
    update = state.model_dump()

    if patch.status is not None:
        update["status"] = patch.status
    if patch.level is not None:
        update["level"] = patch.level
    if patch.plan is not None:
        update["plan"] = patch.plan.model_dump()
    if patch.answer is not None:
        update["answer"] = patch.answer.model_dump()
    if patch.verdict is not None:
        update["verdict"] = patch.verdict.model_dump()
    if patch.budget is not None:
        update["budget"] = patch.budget.model_dump()
    if patch.error is not None:
        update["error"] = patch.error
    if patch.analysis is not None:
        update["analysis"] = {**update["analysis"], **patch.analysis}

    if patch.replace_evidence is not None:
        update["evidence"] = [e.model_dump() for e in patch.replace_evidence]
    elif patch.append_evidence:
        seen: dict[str, dict] = {e["id"]: e for e in update["evidence"]}
        for item in patch.append_evidence:
            seen[item.id] = item.model_dump()
        update["evidence"] = list(seen.values())

    if patch.append_notes:
        update["working_notes"] = [*update["working_notes"], *patch.append_notes]

    if patch.append_invocations:
        invocations: dict[str, dict] = {
            item["id"]: item for item in update["tool_invocations"]
        }
        for item in patch.append_invocations:
            invocations[item.id] = item.model_dump()
        update["tool_invocations"] = list(invocations.values())

    if patch.append_message is not None:
        update["messages"] = [
            *update["messages"],
            patch.append_message.model_dump(),
        ]

    update["step"] = update["step"] + patch.incr_step
    update["repair_round"] = update["repair_round"] + patch.incr_repair
    if patch.last_node is not None:
        update["last_node"] = patch.last_node

    if patch.clear_pending_approval:
        update["pending_approval"] = None
    elif patch.pending_approval is not None:
        update["pending_approval"] = patch.pending_approval.model_dump()
    if patch.resume_node is not None:
        update["resume_node"] = patch.resume_node

    return RunState.model_validate(update)
