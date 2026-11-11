import asyncio

from fastapi.testclient import TestClient

from tests.conftest import make_ctx, make_state
from zcaisagent.api.app import app
from zcaisagent.api.deps import get_container
from zcaisagent.capabilities.tools.gateway import ToolGateway
from zcaisagent.container import create_state
from zcaisagent.domain.enums import ApprovalStatus, AutonomyLevel, RunStatus
from zcaisagent.domain.models import (
    Budget,
    PendingApproval,
    Plan,
    PlanStep,
    RunState,
)
from zcaisagent.orchestration.graph import Edge, GraphSpec
from zcaisagent.orchestration.nodes.act import act_node
from zcaisagent.orchestration.policies.budget import BudgetPolicy
from zcaisagent.orchestration.runtime import Runtime
from zcaisagent.ports.security import AuthContext
from zcaisagent.ports.tools import ToolResult, ToolSpec

client = TestClient(app)

_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}


class FakeWriteTool:
    spec = ToolSpec(
        name="fake_write",
        description="write record",
        input_schema=_SCHEMA,
        risk_level="write",
        scopes=["tool:write"],
        requires_approval=True,
    )

    def __init__(self) -> None:
        self.calls = 0

    async def invoke(self, args: dict, ctx: AuthContext) -> ToolResult:
        self.calls += 1
        return ToolResult(tool=self.spec.name, ok=True, output="written", latency_ms=1)


def _ctx(tool: FakeWriteTool) -> object:
    ctx = make_ctx()
    ctx.auth = AuthContext(
        tenant_id="demo",
        user_id="tester",
        roles=["admin"],
        scopes=["retrieve", "tool:read", "tool:write"],
        acl_tokens=["public"],
    )
    gateway = ToolGateway()
    gateway.register(tool)
    ctx.tools = gateway
    return ctx


def _graph() -> GraphSpec:
    return GraphSpec(
        nodes={"act": act_node},
        edges=[Edge("act", "__end__")],
        entry="act",
        terminals={"__end__": RunStatus.SUCCEEDED},
    )


def _state(budget: Budget | None = None) -> RunState:
    state = make_state("写入一条记录", AutonomyLevel.L2_STANDARD)
    plan = Plan(
        steps=[
            PlanStep(
                id="s1",
                kind="tool",
                target="fake_write",
                query="写入一条记录",
                args={"query": "写入一条记录"},
            )
        ]
    )
    return state.model_copy(update={"plan": plan, "budget": budget or state.budget})


def _decided(state: RunState, status: ApprovalStatus) -> RunState:
    approval = state.pending_approval
    assert approval is not None
    return state.model_copy(
        update={
            "pending_approval": approval.model_copy(
                update={"status": status, "decided_by": "alice"}
            )
        }
    )


def test_write_tool_interrupts_for_approval() -> None:
    tool = FakeWriteTool()
    ctx = _ctx(tool)
    events: list[tuple[str, dict]] = []

    async def emit(event: str, data: dict) -> None:
        events.append((event, data))

    ctx.emit = emit
    state = asyncio.run(Runtime().execute(_graph(), _state(), ctx))

    assert state.status == RunStatus.AWAITING_USER
    approval = state.pending_approval
    assert approval is not None
    assert approval.tool == "fake_write"
    assert approval.step_id == "s1"
    assert approval.risk_level == "write"
    assert approval.args == {"query": "写入一条记录"}
    assert approval.status == ApprovalStatus.PENDING
    assert state.resume_node == "act"
    assert state.analysis["action"]["status"] == "approval_required"
    assert any(note.startswith("approval_requested:") for note in state.working_notes)
    assert tool.calls == 0
    assert [name for name, _ in events].count("approval_required") == 1
    assert events[-1][1]["pending_approval"]["id"] == approval.id


def test_approval_executes_tool_once() -> None:
    tool = FakeWriteTool()
    ctx = _ctx(tool)
    interrupted = asyncio.run(Runtime().execute(_graph(), _state(), ctx))
    resumed = asyncio.run(
        Runtime().resume(_graph(), _decided(interrupted, ApprovalStatus.APPROVED), ctx)
    )

    assert tool.calls == 1
    assert resumed.status == RunStatus.SUCCEEDED
    assert resumed.pending_approval is None
    assert resumed.resume_node is None
    assert [item.status for item in resumed.tool_invocations] == ["ok"]
    assert resumed.tool_invocations[0].tool == "fake_write"
    assert resumed.analysis["action"]["approval_id"] == (
        interrupted.pending_approval.id if interrupted.pending_approval else None
    )
    assert resumed.plan is not None
    assert resumed.plan.steps[0].status == "done"


def test_rejection_skips_tool_and_records_note() -> None:
    tool = FakeWriteTool()
    ctx = _ctx(tool)
    interrupted = asyncio.run(Runtime().execute(_graph(), _state(), ctx))
    approval_id = interrupted.pending_approval.id if interrupted.pending_approval else ""
    resumed = asyncio.run(
        Runtime().resume(_graph(), _decided(interrupted, ApprovalStatus.REJECTED), ctx)
    )

    assert tool.calls == 0
    assert resumed.pending_approval is None
    assert f"approval_rejected:{approval_id}" in resumed.working_notes
    assert resumed.analysis["action"]["status"] == "rejected"
    assert resumed.status == RunStatus.SUCCEEDED
    assert [item.status for item in resumed.tool_invocations] == ["rejected"]


def test_undecided_approval_does_not_resume() -> None:
    tool = FakeWriteTool()
    ctx = _ctx(tool)
    state = _state()
    identical = asyncio.run(Runtime().resume(_graph(), state, ctx))

    assert identical is state
    assert tool.calls == 0


def test_resume_keeps_budget_and_step_counter() -> None:
    tool = FakeWriteTool()
    ctx = _ctx(tool)
    budget = BudgetPolicy.initial(AutonomyLevel.L2_STANDARD).model_copy(
        update={"used_tokens": 1234, "used_steps": 3}
    )
    interrupted = asyncio.run(Runtime().execute(_graph(), _state(budget), ctx))

    assert interrupted.budget.used_tokens == 1234
    assert interrupted.budget.used_steps == 3
    assert interrupted.step == 1

    resumed = asyncio.run(
        Runtime().resume(_graph(), _decided(interrupted, ApprovalStatus.APPROVED), ctx)
    )
    assert resumed.budget.used_tokens == 1234
    assert resumed.budget.used_steps >= 3
    assert resumed.step == interrupted.step + 1


def _save_awaiting(
    user_id: str = "dev",
    tenant_id: str = "demo",
    pending_status: ApprovalStatus = ApprovalStatus.PENDING,
) -> RunState:
    container = get_container()
    state = create_state(
        container,
        goal="写入一条记录",
        tenant_id=tenant_id,
        user_id=user_id,
        level=AutonomyLevel.L2_STANDARD,
    )
    approval = PendingApproval(
        id="ap_test",
        run_id=state.run_id,
        step_id="s1",
        tool="fake_write",
        args={"query": "写入一条记录"},
        risk_level="write",
        status=pending_status,
        decided_by=None if pending_status == ApprovalStatus.PENDING else "alice",
    )
    plan = Plan(
        steps=[
            PlanStep(
                id="s1",
                kind="tool",
                target="fake_write",
                query="写入一条记录",
                args={"query": "写入一条记录"},
            )
        ]
    )
    state = state.model_copy(
        update={
            "status": RunStatus.AWAITING_USER,
            "pending_approval": approval,
            "resume_node": "act",
            "plan": plan,
        }
    )
    asyncio.run(container.checkpoint.save(state))
    return state


def _audit_actions(run_id: str) -> list[str]:
    return [
        event.action
        for event in get_container().audit.list()
        if event.run_id == run_id
    ]


def test_api_approve_returns_hitl_card_and_audits() -> None:
    state = _save_awaiting()
    response = client.post(
        f"/v1/runs/{state.run_id}/approve",
        json={"reason": "同意执行"},
        headers={"X-API-Key": "dev-key"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == state.run_id
    assert body["status"] != RunStatus.AWAITING_USER.value
    assert "approval.approved" in _audit_actions(state.run_id)


def test_api_reject_audits() -> None:
    state = _save_awaiting()
    response = client.post(
        f"/v1/runs/{state.run_id}/reject",
        json={"reason": "不允许写"},
        headers={"X-API-Key": "dev-key"},
    )

    assert response.status_code == 200
    assert response.json()["status"] != RunStatus.AWAITING_USER.value
    assert "approval.rejected" in _audit_actions(state.run_id)


def test_api_cancel_only_for_active_runs() -> None:
    state = _save_awaiting()
    response = client.post(
        f"/v1/runs/{state.run_id}/cancel", headers={"X-API-Key": "dev-key"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == RunStatus.CANCELLED.value
    assert "run.cancelled" in _audit_actions(state.run_id)

    again = client.post(
        f"/v1/runs/{state.run_id}/cancel", headers={"X-API-Key": "dev-key"}
    )
    assert again.status_code == 409


def test_api_resume_requires_decided_approval() -> None:
    pending = _save_awaiting()
    blocked = client.post(
        f"/v1/runs/{pending.run_id}/resume", headers={"X-API-Key": "dev-key"}
    )
    assert blocked.status_code == 409

    approved = _save_awaiting(pending_status=ApprovalStatus.APPROVED)
    resumed = client.post(
        f"/v1/runs/{approved.run_id}/resume", headers={"X-API-Key": "dev-key"}
    )
    assert resumed.status_code == 200
    assert resumed.json()["status"] != RunStatus.AWAITING_USER.value


def test_api_approval_cross_user_is_forbidden() -> None:
    state = _save_awaiting(user_id="alice")
    response = client.post(
        f"/v1/runs/{state.run_id}/approve", headers={"X-API-Key": "dev-key"}
    )
    assert response.status_code == 403


def test_api_approval_requires_pending_approval() -> None:
    container = get_container()
    state = create_state(container, goal="你好", user_id="dev")
    asyncio.run(container.checkpoint.save(state))

    response = client.post(
        f"/v1/runs/{state.run_id}/approve", headers={"X-API-Key": "dev-key"}
    )
    assert response.status_code == 409


def test_api_approval_missing_run_is_not_found() -> None:
    response = client.post(
        "/v1/runs/run_missing/approve", headers={"X-API-Key": "dev-key"}
    )
    assert response.status_code == 404
