import asyncio
from pathlib import Path

from zcaisagent.capabilities.memory.distill import distill_and_write
from zcaisagent.capabilities.memory.policies import write_explicit_memory
from zcaisagent.container import build_container, build_context, create_state
from zcaisagent.domain.models import Message
from zcaisagent.orchestration.build import build_default_graph
from zcaisagent.orchestration.runtime import Runtime
from zcaisagent.ports.memory import MemoryQuery
from zcaisagent.ports.security import AuthContext
from zcaisagent.settings import Settings


def _container(tmp_path: Path):
    return build_container(Settings(data_dir=tmp_path, seed_dir=Path("data/seed")))


async def _auth(container, user_id: str, tenant_id: str = "demo") -> AuthContext:
    return await container.identity.authenticate(
        container.settings.api_key,
        {"tenant_id": tenant_id, "user_id": user_id},
    )


def _run(container, auth: AuthContext, goal: str, history: list[Message] | None = None):
    state = create_state(
        container,
        goal=goal,
        tenant_id=auth.tenant_id,
        user_id=auth.user_id,
        auth=auth,
        history=history,
    )
    ctx = build_context(container, auth)
    asyncio.run(write_explicit_memory(container.memory, goal, ctx))
    result = asyncio.run(Runtime().execute(build_default_graph(), state, ctx))
    return result, ctx


def test_explicit_preference_is_recalled_and_injected(tmp_path: Path) -> None:
    container = _container(tmp_path)
    auth = asyncio.run(_auth(container, "alice"))

    first, _ = _run(container, auth, "记住：以后回答用表格。")
    second, ctx = _run(container, auth, "一线城市住宿标准是多少？", first.messages)

    preferences = (second.analysis.get("memory") or {}).get("preferences") or []
    assert any("表格" in item for item in preferences)
    assert any(note.startswith("memory:injected:") for note in second.working_notes)

    assert asyncio.run(distill_and_write(container.memory, second, ctx)) is True
    episodes = asyncio.run(
        container.memory.read(
            MemoryQuery(
                tenant_id="demo",
                user_id="alice",
                kinds=["episodic"],
            ),
            auth,
        )
    )
    assert episodes
    assert "一线城市住宿标准" in episodes[0].content


def test_memory_is_isolated_by_user_and_tenant(tmp_path: Path) -> None:
    container = _container(tmp_path)
    alice = asyncio.run(_auth(container, "alice"))
    bob = asyncio.run(_auth(container, "bob"))
    outsider = asyncio.run(_auth(container, "alice", tenant_id="other"))

    _run(container, alice, "记住：以后回答用表格。")

    def _prefs(auth: AuthContext) -> list[str]:
        items = asyncio.run(
            container.memory.read(
                MemoryQuery(
                    tenant_id=auth.tenant_id,
                    user_id=auth.user_id,
                    kinds=["semantic"],
                ),
                auth,
            )
        )
        return [item.content for item in items]

    assert _prefs(alice)
    assert _prefs(bob) == []
    assert _prefs(outsider) == []
