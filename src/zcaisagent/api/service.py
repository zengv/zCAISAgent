from zcaisagent.api.deps import get_container
from zcaisagent.api.schemas import RunRequest
from zcaisagent.container import build_context, create_state
from zcaisagent.domain.enums import AutonomyLevel
from zcaisagent.domain.models import Message, RunState
from zcaisagent.orchestration.build import build_default_graph
from zcaisagent.orchestration.runtime import Runtime
from zcaisagent.ports.security import AuditEvent, AuthContext

# 多轮上下文保留最近消息窗口，避免历史无限增长（完整压缩策略见 FR-MEM-01）。
_HISTORY_WINDOW = 6


async def _thread_history(
    auth: AuthContext, thread_id: str, tenant_id: str
) -> list[Message]:
    container = get_container()
    runs = await container.checkpoint.list_thread(thread_id)
    if not runs:
        return []
    latest = max(runs, key=lambda state: (state.created_ts, state.run_id))
    if latest.tenant_id != tenant_id or latest.user_id != auth.user_id:
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


async def execute_run(auth: AuthContext, request: RunRequest) -> RunState:
    container = get_container()
    state = await build_run_state(auth, request)
    ctx = build_context(container, auth)
    runtime = Runtime()
    result = await runtime.execute(build_default_graph(), state, ctx)
    await container.audit.append(
        AuditEvent(
            action="run.finished",
            actor=auth.user_id,
            tenant_id=auth.tenant_id,
            run_id=result.run_id,
            detail={"status": result.status.value, "steps": result.step},
        )
    )
    return result
