import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from zcaisagent.api import deps
from zcaisagent.api.app import app
from zcaisagent.container import build_container, build_context, create_state
from zcaisagent.domain.enums import AutonomyLevel, RunStatus
from zcaisagent.governance.audit import EnvSecret
from zcaisagent.governance.identity import ApiKeyIdentity, RbacPolicy
from zcaisagent.orchestration.build import build_default_graph
from zcaisagent.orchestration.runtime import Runtime
from zcaisagent.settings import Settings


def _container(tmp_path: Path):
    return build_container(Settings(data_dir=tmp_path, seed_dir=Path("data/seed")))


def _auth(container):
    return asyncio.run(
        container.identity.authenticate(
            container.settings.api_key, {"tenant_id": "demo"}
        )
    )


def test_default_container_wiring(tmp_path: Path) -> None:
    container = _container(tmp_path)

    assert container.cache is None
    assert container.events is None
    assert container.memory is not None
    assert "kb_search" in {spec.name for spec in container.tools.all()}
    assert isinstance(container.identity, ApiKeyIdentity)
    assert isinstance(container.policy, RbacPolicy)
    assert isinstance(container.secret, EnvSecret)


def test_default_context_injects_m2_ports(tmp_path: Path) -> None:
    container = _container(tmp_path)
    ctx = build_context(container, _auth(container))

    for name in (
        "llm",
        "retriever",
        "checkpoint",
        "trace",
        "auth",
        "config",
        "sources",
        "reranker",
        "health",
        "guardrail",
        "skills",
        "tools",
        "memory",
        "cache",
        "events",
        "audit",
        "node_timeout_s",
    ):
        assert hasattr(ctx, name), name
    assert ctx.tools is not None
    assert ctx.memory is not None
    assert ctx.node_timeout_s > 0


def test_l2_run_checkpoint_and_health(tmp_path: Path, monkeypatch) -> None:
    container = _container(tmp_path)
    auth = _auth(container)
    state = create_state(
        container,
        "一线城市住宿标准是多少？",
        level=AutonomyLevel.L2_STANDARD,
        auth=auth,
    )

    result = asyncio.run(
        Runtime().execute(build_default_graph(), state, build_context(container, auth))
    )

    assert result.status in {RunStatus.SUCCEEDED, RunStatus.ABSTAINED}
    loaded = asyncio.run(container.checkpoint.load(result.run_id))
    assert loaded is not None
    assert loaded.run_id == result.run_id

    monkeypatch.setattr(deps, "build_container", lambda: container)
    deps.get_container.cache_clear()
    client = TestClient(app)
    try:
        assert client.get("/v1/admin/cache/stats").status_code == 200
        response = client.get("/health")
        assert response.status_code == 200
        entries = {
            entry["name"]: entry["status"] for entry in response.json()["entries"]
        }
        assert entries["secret"] == "ok"
        assert entries["events"] == "ok"
    finally:
        deps.get_container.cache_clear()
