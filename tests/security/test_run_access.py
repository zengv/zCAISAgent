import asyncio
from uuid import uuid4

from fastapi.testclient import TestClient

from zcaisagent.api.app import app
from zcaisagent.domain.models import RunState

client = TestClient(app)


def _save_run(container, user_id: str, tenant_id: str = "demo") -> str:
    run_id = f"run_{uuid4().hex[:10]}"
    state = RunState(
        run_id=run_id,
        thread_id=f"thread_{run_id}",
        tenant_id=tenant_id,
        user_id=user_id,
        goal="住宿标准是多少？",
    )
    asyncio.run(container.checkpoint.save(state))
    return run_id


def test_run_read_requires_ownership(offline_api_container) -> None:
    run_id = _save_run(offline_api_container, "alice")

    denied = client.get(f"/v1/runs/{run_id}")
    assert denied.status_code == 403

    allowed = client.get(f"/v1/runs/{run_id}", params={"user_id": "alice"})
    assert allowed.status_code == 200
    assert allowed.json()["run_id"] == run_id


def test_trace_read_requires_ownership(offline_api_container) -> None:
    run_id = _save_run(offline_api_container, "alice")

    denied = client.get(f"/v1/runs/{run_id}/trace")
    assert denied.status_code == 403

    allowed = client.get(
        f"/v1/runs/{run_id}/trace", params={"user_id": "alice"}
    )
    assert allowed.status_code == 200


def test_cross_tenant_read_is_audited(offline_api_container) -> None:
    run_id = _save_run(offline_api_container, "alice", tenant_id="other")

    response = client.get(
        f"/v1/runs/{run_id}", params={"user_id": "alice", "tenant_id": "demo"}
    )
    assert response.status_code == 403

    events = [e for e in offline_api_container.audit.list() if e.run_id == run_id]
    assert any(e.action == "run.cross_tenant_denied" for e in events)


def test_missing_run_is_404() -> None:
    assert client.get("/v1/runs/run_missing").status_code == 404
