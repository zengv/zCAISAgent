from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException

from zcaisagent.api.deps import get_container
from zcaisagent.api.schemas import ApprovalDecision, RunResponse
from zcaisagent.api.service import to_response
from zcaisagent.container import build_context
from zcaisagent.domain.enums import ApprovalStatus, RunStatus
from zcaisagent.domain.events import RunEvent
from zcaisagent.domain.models import PendingApproval, RunState
from zcaisagent.orchestration.build import build_default_graph
from zcaisagent.orchestration.runtime import Runtime
from zcaisagent.ports.security import AuditEvent, AuthContext

router = APIRouter()


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
    x_api_key: str = Header(default="dev-key", alias="X-API-Key"),
):
    container = get_container()
    auth = await container.identity.authenticate(
        x_api_key, {"tenant_id": tenant_id or "", "user_id": user_id or ""}
    )
    state = await _load_owned(run_id, auth)
    return state.model_dump()


@router.get("/runs/{run_id}/trace")
async def get_trace(
    run_id: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
    x_api_key: str = Header(default="dev-key", alias="X-API-Key"),
):
    container = get_container()
    auth = await container.identity.authenticate(
        x_api_key, {"tenant_id": tenant_id or "", "user_id": user_id or ""}
    )
    await _load_owned(run_id, auth)
    events = container.trace.events(run_id)
    if not events:
        path = container.settings.data_dir / "trace.jsonl"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event = RunEvent.model_validate_json(line)
                if event.run_id == run_id:
                    events.append(event)
    return {"run_id": run_id, "events": [e.model_dump() for e in events]}


async def _load_owned(run_id: str, auth: AuthContext) -> RunState:
    container = get_container()
    state = await container.checkpoint.load(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="run not found")
    if state.tenant_id != auth.tenant_id or state.user_id != auth.user_id:
        await container.audit.append(
            AuditEvent(
                action="run.cross_tenant_denied",
                actor=auth.user_id,
                tenant_id=auth.tenant_id,
                run_id=run_id,
                detail={
                    "owner_tenant": state.tenant_id,
                    "owner_user": state.user_id,
                    "reason": "run_not_accessible",
                },
            )
        )
        raise HTTPException(status_code=403, detail="run not accessible")
    return state


def _decided_approval(
    approval: PendingApproval,
    status: ApprovalStatus,
    body: ApprovalDecision | None,
    auth: AuthContext,
) -> PendingApproval:
    return approval.model_copy(
        update={
            "status": status,
            "decided_at": datetime.now(UTC),
            "decided_by": (body.actor if body and body.actor else auth.user_id),
            "reason": body.reason if body else "",
        }
    )


async def _record_decision(
    state: RunState, approval: PendingApproval, status: ApprovalStatus, auth: AuthContext
) -> None:
    container = get_container()
    await container.audit.append(
        AuditEvent(
            action=f"approval.{status.value}",
            actor=auth.user_id,
            tenant_id=auth.tenant_id,
            run_id=state.run_id,
            detail={
                "approval_id": approval.id,
                "tool": approval.tool,
                "risk_level": approval.risk_level,
                "decision": status.value,
                "decided_by": approval.decided_by,
                "reason": approval.reason,
            },
        )
    )


async def _decide(
    run_id: str,
    status: ApprovalStatus,
    body: ApprovalDecision | None,
    x_api_key: str,
) -> RunResponse:
    container = get_container()
    auth = await container.identity.authenticate(x_api_key, {})
    state = await _load_owned(run_id, auth)
    approval = state.pending_approval
    if state.status != RunStatus.AWAITING_USER or approval is None:
        raise HTTPException(status_code=409, detail="run has no pending approval")
    if approval.status != ApprovalStatus.PENDING:
        raise HTTPException(status_code=409, detail="approval already decided")

    decided = _decided_approval(approval, status, body, auth)
    state = state.model_copy(update={"pending_approval": decided})
    await container.checkpoint.save(state)
    await _record_decision(state, decided, status, auth)

    result = await Runtime().resume(
        build_default_graph(), state, build_context(container, auth)
    )
    return to_response(result)


@router.post("/runs/{run_id}/approve", response_model=RunResponse)
async def approve_run(
    run_id: str,
    body: ApprovalDecision | None = None,
    x_api_key: str = Header(default="dev-key", alias="X-API-Key"),
) -> RunResponse:
    return await _decide(run_id, ApprovalStatus.APPROVED, body, x_api_key)


@router.post("/runs/{run_id}/reject", response_model=RunResponse)
async def reject_run(
    run_id: str,
    body: ApprovalDecision | None = None,
    x_api_key: str = Header(default="dev-key", alias="X-API-Key"),
) -> RunResponse:
    return await _decide(run_id, ApprovalStatus.REJECTED, body, x_api_key)


@router.post("/runs/{run_id}/cancel", response_model=RunResponse)
async def cancel_run(
    run_id: str,
    x_api_key: str = Header(default="dev-key", alias="X-API-Key"),
) -> RunResponse:
    container = get_container()
    auth = await container.identity.authenticate(x_api_key, {})
    state = await _load_owned(run_id, auth)
    if state.status not in {RunStatus.AWAITING_USER, RunStatus.RUNNING}:
        raise HTTPException(status_code=409, detail="run cannot be cancelled")
    previous = state.status
    state = state.model_copy(update={"status": RunStatus.CANCELLED})
    await container.checkpoint.save(state)
    await container.audit.append(
        AuditEvent(
            action="run.cancelled",
            actor=auth.user_id,
            tenant_id=auth.tenant_id,
            run_id=run_id,
            detail={"previous_status": previous.value},
        )
    )
    return to_response(state)


@router.post("/runs/{run_id}/resume", response_model=RunResponse)
async def resume_run(
    run_id: str,
    x_api_key: str = Header(default="dev-key", alias="X-API-Key"),
) -> RunResponse:
    container = get_container()
    auth = await container.identity.authenticate(x_api_key, {})
    state = await _load_owned(run_id, auth)
    approval = state.pending_approval
    if state.status != RunStatus.AWAITING_USER or approval is None:
        raise HTTPException(status_code=409, detail="run has no pending approval")
    if approval.status not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
        raise HTTPException(status_code=409, detail="approval is still pending")

    result = await Runtime().resume(
        build_default_graph(), state, build_context(container, auth)
    )
    return to_response(result)
