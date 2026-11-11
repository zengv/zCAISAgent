from zcaisagent.api.deps import get_container
from zcaisagent.api.schemas import (
    CitationOut,
    RunRequest,
    RunResponse,
    build_approval,
    build_metrics,
)
from zcaisagent.capabilities.memory.distill import distill_and_write
from zcaisagent.capabilities.memory.policies import write_explicit_memory
from zcaisagent.container import build_context, create_state
from zcaisagent.domain.enums import AutonomyLevel, RunStatus
from zcaisagent.domain.models import Message, RunState
from zcaisagent.domain.stage import ticket_stage
from zcaisagent.orchestration.build import build_default_graph
from zcaisagent.orchestration.runtime import Runtime
from zcaisagent.ports.events import EventEnvelope, EventSinkPort
from zcaisagent.ports.security import AuditEvent, AuthContext

# 多轮上下文保留最近消息窗口，避免历史无限增长（完整压缩策略见 FR-MEM-01）。
_HISTORY_WINDOW = 6


def to_response(state: RunState) -> RunResponse:
    answer = state.answer
    return RunResponse(
        run_id=state.run_id,
        thread_id=state.thread_id,
        status=state.status.value,
        stage=ticket_stage(state).value,
        answer=answer.text if answer else "",
        confidence=answer.confidence if answer else "low",
        abstained=answer.abstained if answer else True,
        citations=[
            CitationOut(
                evidence_id=citation.evidence_id,
                quote=citation.quote,
                confidence=citation.confidence,
            )
            for citation in (answer.citations if answer else [])
        ],
        evidence_count=len(state.evidence),
        steps=state.step,
        mandate_id=state.mandate.id if state.mandate else None,
        limitations=answer.limitations if answer else [],
        metrics=build_metrics(state),
        approval=build_approval(state),
    )


async def _thread_history(
    auth: AuthContext, thread_id: str, tenant_id: str
) -> list[Message]:
    container = get_container()
    runs = await container.checkpoint.list_thread(thread_id)
    if not runs:
        return []
    latest = max(runs, key=lambda state: (state.created_ts, state.run_id))
    if latest.tenant_id != tenant_id or latest.user_id != auth.user_id:
        await container.audit.append(
            AuditEvent(
                action="thread.cross_tenant_denied",
                actor=auth.user_id,
                tenant_id=auth.tenant_id,
                run_id=latest.run_id,
                detail={
                    "thread_id": thread_id,
                    "owner_tenant": latest.tenant_id,
                    "owner_user": latest.user_id,
                    "reason": "thread_not_owned",
                },
            )
        )
        raise PermissionError("thread_not_owned")
    history = list(latest.messages)
    if latest.answer is not None and not latest.answer.abstained:
        history.append(Message(role="assistant", content=latest.answer.text))
    return history[-_HISTORY_WINDOW:]


async def build_run_state(auth: AuthContext, request: RunRequest) -> RunState:
    """认证后的运行态构建：校验 goal 并装配多轮上下文。"""
    goal = request.goal.strip()
    if not goal:
        raise ValueError("goal must not be empty")

    container = get_container()
    level = AutonomyLevel.L1_FAST
    if request.level:
        try:
            level = AutonomyLevel(request.level)
        except ValueError:
            level = AutonomyLevel.L1_FAST

    tenant_id = request.tenant_id or auth.tenant_id or container.settings.default_tenant
    history = (
        await _thread_history(auth, request.thread_id, tenant_id)
        if request.thread_id
        else []
    )
    return create_state(
        container,
        goal=goal,
        thread_id=request.thread_id,
        tenant_id=tenant_id,
        user_id=request.user_id,
        level=level,
        auth=auth,
        history=history,
    )


async def _publish(events: EventSinkPort | None, event: EventEnvelope) -> None:
    if events is None:
        return
    try:
        await events.publish(event)
    except Exception:  # noqa: BLE001 - 事件投递失败不阻断主流程
        return


async def publish_run_events(
    events: EventSinkPort | None, state: RunState
) -> None:
    """发布 Run 结束事件；AWAITING_USER 时追加 approval.requested。"""
    if events is None:
        return
    answer = state.answer
    await _publish(
        events,
        EventEnvelope(
            type="run.finished",
            tenant_id=state.tenant_id,
            run_id=state.run_id,
            data={
                "status": state.status.value,
                "steps": state.step,
                "answer_abstained": answer.abstained if answer else True,
                "evidence_count": len(state.evidence),
            },
        ),
    )
    approval = state.pending_approval
    if state.status == RunStatus.AWAITING_USER and approval is not None:
        await _publish(
            events,
            EventEnvelope(
                type="approval.requested",
                tenant_id=state.tenant_id,
                run_id=state.run_id,
                data={
                    "approval_id": approval.id,
                    "tool": approval.tool,
                    "risk_level": approval.risk_level,
                },
            ),
        )


async def execute_run(auth: AuthContext, request: RunRequest) -> RunState:
    container = get_container()
    state = await build_run_state(auth, request)
    ctx = build_context(container, auth)
    await write_explicit_memory(container.memory, state.goal, ctx)
    runtime = Runtime()
    result = await runtime.execute(build_default_graph(), state, ctx)
    await distill_and_write(container.memory, result, ctx)
    await container.audit.append(
        AuditEvent(
            action="run.finished",
            actor=auth.user_id,
            tenant_id=auth.tenant_id,
            run_id=result.run_id,
            detail={"status": result.status.value, "steps": result.step},
        )
    )
    await publish_run_events(container.events, result)
    return result
