import asyncio
from pathlib import Path

from zcaisagent.api.deps import get_container
from zcaisagent.api.schemas import RunRequest
from zcaisagent.api.service import execute_run, publish_run_events
from zcaisagent.container import build_container, create_state
from zcaisagent.domain.enums import AutonomyLevel, RunStatus
from zcaisagent.domain.models import PendingApproval
from zcaisagent.governance.audit import EnvSecret
from zcaisagent.ports.events import EventEnvelope
from zcaisagent.ports.health import HealthStatus
from zcaisagent.settings import Settings


class FakeEventSink:
    def __init__(self) -> None:
        self.events: list[EventEnvelope] = []

    async def publish(self, event: EventEnvelope) -> None:
        self.events.append(event)


class FailingEventSink:
    async def publish(self, event: EventEnvelope) -> None:
        raise RuntimeError("sink down")


def test_execute_run_publishes_finished_event() -> None:
    container = get_container()
    sink = FakeEventSink()
    original = container.events
    container.events = sink
    try:
        auth = asyncio.run(
            container.identity.authenticate(
                "dev-key", {"tenant_id": "demo", "user_id": "dev"}
            )
        )
        state = asyncio.run(execute_run(auth, RunRequest(goal="一线城市住宿标准是多少？")))
    finally:
        container.events = original

    assert state.status == RunStatus.SUCCEEDED
    assert [event.type for event in sink.events] == ["run.finished"]
    event = sink.events[0]
    assert event.run_id == state.run_id
    assert event.tenant_id == state.tenant_id
    assert event.data["status"] == "succeeded"
    assert event.data["steps"] == state.step
    assert event.data["evidence_count"] == len(state.evidence)
    assert event.data["answer_abstained"] is False


def test_awaiting_run_publishes_approval_requested() -> None:
    container = get_container()
    sink = FakeEventSink()
    state = create_state(
        container,
        goal="写入一条记录",
        level=AutonomyLevel.L2_STANDARD,
    )
    approval = PendingApproval(
        id="ap_1",
        run_id=state.run_id,
        step_id="s1",
        tool="ticket_create",
        risk_level="write",
    )
    awaiting = state.model_copy(
        update={"status": RunStatus.AWAITING_USER, "pending_approval": approval}
    )

    asyncio.run(publish_run_events(sink, awaiting))

    assert [event.type for event in sink.events] == [
        "run.finished",
        "approval.requested",
    ]
    finished, requested = sink.events
    assert finished.data["status"] == "awaiting_user"
    assert requested.data == {
        "approval_id": "ap_1",
        "tool": "ticket_create",
        "risk_level": "write",
    }


def test_publish_failure_does_not_block_run() -> None:
    container = get_container()
    original = container.events
    container.events = FailingEventSink()
    try:
        auth = asyncio.run(
            container.identity.authenticate(
                "dev-key", {"tenant_id": "demo", "user_id": "dev"}
            )
        )
        state = asyncio.run(execute_run(auth, RunRequest(goal="婚假有几天？")))
    finally:
        container.events = original

    assert state.status == RunStatus.SUCCEEDED


def test_default_container_disables_events_and_uses_env_secret(tmp_path: Path) -> None:
    container = build_container(
        Settings(data_dir=tmp_path, seed_dir=Path("data/seed")),
        ingest=False,
    )

    assert container.events is None
    assert isinstance(container.secret, EnvSecret)
    entries = {entry.name: entry for entry in container.health.snapshot()}
    assert entries["events"].status == HealthStatus.OK
    assert entries["events"].message == "disabled"
    assert entries["secret"].status == HealthStatus.OK
