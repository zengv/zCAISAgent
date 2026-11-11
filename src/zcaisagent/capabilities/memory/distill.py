from zcaisagent.capabilities.memory.policies import prepare_write
from zcaisagent.domain.enums import MemoryKind, RunStatus
from zcaisagent.domain.models import RunState
from zcaisagent.ports.memory import MemoryItem, MemorySource

_GOAL_MAX_CHARS = 120
_ANSWER_MAX_CHARS = 200
_CONTENT_MAX_CHARS = 500


def _answer_summary(state: RunState) -> str:
    answer = state.answer
    if state.status == RunStatus.ABSTAINED:
        reason = state.analysis.get("abstain_reason") or ""
        if not reason and state.verdict is not None and state.verdict.issues:
            reason = state.verdict.issues[0]
        if not reason and answer is not None and answer.limitations:
            reason = answer.limitations[0]
        return f"拒答原因：{reason or '证据不足'}"[:_ANSWER_MAX_CHARS]
    text = answer.text.strip() if answer is not None else ""
    return (text or "无结论")[:_ANSWER_MAX_CHARS]


def distill_episode(state: RunState) -> MemoryItem | None:
    """Run 结束后生成一条低风险 episodic 摘要，不携带证据正文。"""
    if state.status not in {RunStatus.SUCCEEDED, RunStatus.ABSTAINED}:
        return None
    goal = state.goal.strip()[:_GOAL_MAX_CHARS]
    intent = str(state.analysis.get("intent") or "unknown")
    complexity = str(state.analysis.get("complexity") or "unknown")
    content = (
        f"目标：{goal}；意图：{intent}/{complexity}；步数：{state.step}；"
        f"结论：{_answer_summary(state)}"
    )[:_CONTENT_MAX_CHARS]
    return MemoryItem(
        id=f"ep_{state.run_id}",
        kind=MemoryKind.EPISODIC.value,
        content=content,
        metadata={
            "run_id": state.run_id,
            "status": state.status.value,
            "level": state.level.value,
        },
    )


async def distill_and_write(memory, state: RunState, ctx) -> bool:
    """蒸馏并写入 episodic 记忆；任何异常都吞掉，不阻断主流程。"""
    try:
        if memory is None:
            return False
        config = getattr(getattr(ctx, "config", None), "memory", None)
        if config is not None and (not config.enabled or not config.distill_enabled):
            return False
        item = distill_episode(state)
        if item is None:
            return False
        auth = getattr(ctx, "auth", ctx)
        prepared = prepare_write(item, MemorySource.DISTILL, False, auth)
        if prepared is None:
            return False
        await memory.write(prepared, auth)
    except Exception:  # noqa: BLE001 - 蒸馏失败不影响 Run 结果
        return False
    return True
