import time
import uuid
from dataclasses import dataclass

from zcaisagent.adapters.checkpoint.jsonl import JsonlCheckpoint
from zcaisagent.adapters.health.memory import HealthBoard
from zcaisagent.adapters.llm.fake import FakeLLM
from zcaisagent.adapters.llm.openai_compat import OpenAICompatLLM
from zcaisagent.adapters.trace.jsonl import JsonlTrace
from zcaisagent.capabilities.retrieval.registry import SourceRegistry
from zcaisagent.capabilities.retrieval.sources.inmemory import InMemoryRetriever
from zcaisagent.capabilities.skills.registry import (
    InMemorySkillRegistry,
    build_default_skill_registry,
)
from zcaisagent.capabilities.tools.gateway import ToolGateway, build_default_tool_gateway
from zcaisagent.domain.enums import AutonomyLevel
from zcaisagent.domain.mandate import Mandate
from zcaisagent.domain.models import Message, RunState
from zcaisagent.governance.audit import EnvSecret, JsonlAudit
from zcaisagent.governance.guards import RuleGuardrail
from zcaisagent.governance.identity import ApiKeyIdentity, RbacPolicy
from zcaisagent.ingestion.pipeline import ingest_directory
from zcaisagent.orchestration.policies.budget import BudgetPolicy
from zcaisagent.orchestration.runtime import RunContext
from zcaisagent.ports.checkpoint import CheckpointPort
from zcaisagent.ports.health import HealthStatus
from zcaisagent.ports.llm import LLMPort
from zcaisagent.ports.security import AuthContext
from zcaisagent.settings import Settings


@dataclass
class Container:
    settings: Settings
    retriever: InMemoryRetriever
    sources: SourceRegistry
    llm: LLMPort
    checkpoint: CheckpointPort
    trace: JsonlTrace
    audit: JsonlAudit
    identity: ApiKeyIdentity
    policy: RbacPolicy
    guardrail: RuleGuardrail
    secret: EnvSecret
    health: HealthBoard
    skills: InMemorySkillRegistry
    tools: ToolGateway


def build_container(settings: Settings | None = None, ingest: bool = True) -> Container:
    settings = settings or Settings.from_env()
    retriever = InMemoryRetriever()
    if ingest:
        retriever.add(ingest_directory(settings.seed_dir, tenant_id=settings.default_tenant))
    sources = SourceRegistry()
    sources.register(retriever)

    if settings.model_providers:
        llm: LLMPort = OpenAICompatLLM(
            settings=settings,
            timeout_s=settings.node_timeout_s,
        )
    else:
        llm = FakeLLM()

    health = HealthBoard()
    for name in ("runtime", "llm", "retrieval", "checkpoint", "trace"):
        health.register(name, name)
        health.report(name, HealthStatus.OK)

    audit = JsonlAudit(settings.data_dir / "audit.jsonl")

    return Container(
        settings=settings,
        retriever=retriever,
        sources=sources,
        llm=llm,
        checkpoint=JsonlCheckpoint(settings.data_dir / "checkpoints"),
        trace=JsonlTrace(settings.data_dir / "trace.jsonl"),
        audit=audit,
        identity=ApiKeyIdentity(settings.api_key, settings.default_tenant),
        policy=RbacPolicy(),
        guardrail=RuleGuardrail(),
        secret=EnvSecret(
            {
                "api_key": settings.api_key,
            }
        ),
        health=health,
        skills=build_default_skill_registry(),
        tools=build_default_tool_gateway(audit=audit, retriever=retriever),
    )


def create_state(
    container: Container,
    goal: str,
    thread_id: str | None = None,
    tenant_id: str | None = None,
    user_id: str = "dev",
    level: AutonomyLevel | None = None,
    run_id: str | None = None,
    auth: AuthContext | None = None,
    history: list[Message] | None = None,
) -> RunState:
    settings = container.settings
    requested_level = level
    level = level or AutonomyLevel.L1_FAST
    run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
    mandate = None
    if auth is not None:
        mandate = Mandate(
            id=f"md_{run_id}",
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            scopes=auth.scopes,
            acl_tokens=auth.acl_tokens,
            basis="api_request",
        )
    return RunState(
        run_id=run_id,
        thread_id=thread_id or run_id,
        tenant_id=tenant_id or settings.default_tenant,
        user_id=user_id,
        goal=goal,
        created_ts=time.time(),
        level=level,
        requested_level=requested_level,
        mandate=mandate,
        messages=[*(history or []), Message(role="user", content=goal)],
        budget=BudgetPolicy.initial(level),
    )


def build_context(container: Container, auth: AuthContext) -> RunContext:
    return RunContext(
        llm=container.llm,
        retriever=container.retriever,
        checkpoint=container.checkpoint,
        trace=container.trace,
        auth=auth,
        config=container.settings,
        sources=container.sources,
        health=container.health,
        guardrail=container.guardrail,
        skills=container.skills,
        tools=container.tools,
        node_timeout_s=container.settings.node_timeout_s,
    )
