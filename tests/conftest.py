import os
from pathlib import Path
from unittest import mock

import pytest

from zcaisagent.adapters.checkpoint.jsonl import InMemoryCheckpoint
from zcaisagent.adapters.llm.fake import FakeLLM
from zcaisagent.adapters.trace.jsonl import JsonlTrace
from zcaisagent.capabilities.retrieval.sources.inmemory import InMemoryRetriever
from zcaisagent.container import build_container
from zcaisagent.domain.enums import AutonomyLevel
from zcaisagent.domain.models import Chunk, Message, RunState
from zcaisagent.orchestration.build import build_default_graph
from zcaisagent.orchestration.policies.budget import BudgetPolicy
from zcaisagent.orchestration.runtime import RunContext, Runtime
from zcaisagent.ports.security import AuthContext
from zcaisagent.ports.skills import SkillRegistryPort
from zcaisagent.settings import Settings

# 测试不读取项目 .env，避免真实模型与密钥影响用例。
os.environ.setdefault("ZCAISAGENT_ENV_FILE", str(Path(__file__).with_name(".env.test")))


@pytest.fixture(scope="session", autouse=True)
def offline_api_container(tmp_path_factory):
    """API 集成测试统一使用离线 FakeLLM 容器，避免依赖真实模型密钥。"""
    from zcaisagent.api import deps

    container = build_container(
        Settings(
            data_dir=tmp_path_factory.mktemp("api"),
            seed_dir=Path("data/seed"),
        )
    )
    with mock.patch.object(deps, "build_container", return_value=container):
        deps.get_container.cache_clear()
        yield container
        deps.get_container.cache_clear()


def make_state(goal: str, level: AutonomyLevel = AutonomyLevel.L1_FAST) -> RunState:
    return RunState(
        run_id="run_test",
        thread_id="thread_test",
        tenant_id="demo",
        user_id="tester",
        goal=goal,
        level=level,
        messages=[Message(role="user", content=goal)],
        budget=BudgetPolicy.initial(level),
    )


def make_auth() -> AuthContext:
    return AuthContext(
        tenant_id="demo",
        user_id="tester",
        roles=["admin"],
        scopes=["retrieve"],
        acl_tokens=["public"],
    )


def make_chunk(chunk_id: str, content: str, acl: list[str] | None = None) -> Chunk:
    return Chunk(
        id=chunk_id,
        doc_id=chunk_id.split("#")[0],
        title="测试文档",
        content=content,
        index=0,
        tenant_id="demo",
        acl=acl or ["public"],
    )


def make_ctx(
    chunks: list[Chunk] | None = None,
    tmp_path: Path | None = None,
    top_k: int = 5,
    skills: SkillRegistryPort | None = None,
) -> RunContext:
    retriever = InMemoryRetriever()
    if chunks:
        retriever.add(chunks)
    trace_path = (tmp_path or Path(".zcaisagent-test")) / "trace.jsonl"
    return RunContext(
        llm=FakeLLM(),
        retriever=retriever,
        checkpoint=InMemoryCheckpoint(),
        trace=JsonlTrace(trace_path),
        auth=make_auth(),
        config=Settings(top_k=top_k, data_dir=tmp_path or Path(".zcaisagent-test")),
        skills=skills,
    )


async def run_graph(ctx: RunContext, state: RunState) -> RunState:
    return await Runtime().execute(build_default_graph(), state, ctx)
