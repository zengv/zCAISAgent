import time
import uuid
from dataclasses import dataclass

from zcaisagent.adapters.checkpoint.jsonl import JsonlCheckpoint
from zcaisagent.adapters.health.memory import HealthBoard
from zcaisagent.adapters.llm.fake import FakeLLM
from zcaisagent.adapters.llm.openai_compat import OpenAICompatEmbedding, OpenAICompatLLM
from zcaisagent.adapters.trace.jsonl import JsonlTrace
from zcaisagent.capabilities.retrieval.registry import SourceRegistry
from zcaisagent.capabilities.retrieval.rerank import (
    EmbeddingReranker,
    FallbackReranker,
    LLMPointwiseReranker,
    OpenAICompatRerank,
)
from zcaisagent.capabilities.retrieval.sources.bm25 import SqliteBm25Retriever
from zcaisagent.capabilities.retrieval.sources.graph import GraphRetriever
from zcaisagent.capabilities.retrieval.sources.inmemory import InMemoryRetriever
from zcaisagent.capabilities.retrieval.sources.pageindex import PageIndexRetriever
from zcaisagent.capabilities.retrieval.sources.vector import VectorRetriever
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
    # 融合后 LLM 相关性重排；None 表示关闭（回退词法相关性逻辑）。
    reranker: LLMPointwiseReranker | None = None


def build_container(settings: Settings | None = None, ingest: bool = True) -> Container:
    settings = settings or Settings.from_env()
    health = HealthBoard()
    for name in ("runtime", "llm", "retrieval", "checkpoint", "trace"):
        health.register(name, name)
        health.report(name, HealthStatus.OK)

    retriever = InMemoryRetriever()
    store = None
    stored_chunks = []
    database = settings.database
    if database is not None and database.enabled:
        try:
            from zcaisagent.adapters.store.postgres import PsycopgVectorStore

            store = PsycopgVectorStore(database.dsn, database.dimensions)
            store.ensure_schema()
            stored_chunks = store.load_chunks()
            health.register("database", "database")
            health.report("database", HealthStatus.OK)
        except Exception as exc:  # noqa: BLE001 - PG 是可选加速层，失败回退内存
            store = None
            stored_chunks = []
            health.register("database", "database")
            health.report("database", HealthStatus.DOWN, str(exc))
    if ingest:
        if stored_chunks:
            # PG 已有索引：直接载入，跳过重复 ingest。
            retriever.add(stored_chunks)
        else:
            retriever.add(
                ingest_directory(
                    settings.seed_dir,
                    tenant_id=settings.default_tenant,
                    parent_max_chars=(
                        settings.parent_max_chars
                        if settings.parent_child_enabled
                        else None
                    ),
                )
            )
    sources = SourceRegistry()
    sources.register(retriever)
    # 关键词路与词法路共用同一批 chunk，由 retrieve 节点按 RRF 融合。
    bm25 = SqliteBm25Retriever()
    bm25.add(retriever.chunks)
    sources.register(bm25, priority=110)

    if settings.model_providers:
        llm: LLMPort = OpenAICompatLLM(
            settings=settings,
            timeout_s=settings.node_timeout_s,
        )
    else:
        llm = FakeLLM()
    # rerank 装配：[rerank] 配置了专用端点时优先 /rerank API，网关不支持
    # （404/超时）自动回退——有 embedding 走语义余弦，否则 LLM chat 打分。
    reranker = None
    embedder = None
    if settings.embedding is not None:
        embedder = OpenAICompatEmbedding(settings.embedding, timeout_s=settings.node_timeout_s)
    semantic_reranker = (
        EmbeddingReranker(embedder, timeout_s=settings.node_timeout_s)
        if embedder is not None
        else LLMPointwiseReranker(llm, timeout_s=settings.node_timeout_s)
    )
    if settings.rerank is not None:
        reranker = FallbackReranker(
            OpenAICompatRerank(settings.rerank, timeout_s=settings.node_timeout_s),
            semantic_reranker,
        )
    elif settings.rerank_enabled:
        reranker = semantic_reranker

    # 语义向量路：embedding 服务启用时注册；服务不可用时该源运行中自动降级。
    if embedder is not None:
        if store is not None:
            from zcaisagent.capabilities.retrieval.sources.pgvector import PgVectorRetriever

            vector = PgVectorRetriever(store, embedder)
        else:
            vector = VectorRetriever(embedder)
            vector.add(retriever.chunks)
        sources.register(vector, priority=110)

    # 结构化召回路：PageIndex 章节树导航 + GraphRAG 实体图关联。
    # 消融实验（data/eval/combo_report2.txt）显示两源进入常规融合均为负贡献
    # （等权与降权皆然），故默认停用，仅保留 --source 单源评测与显式启用；
    # 实体图带磁盘缓存与失败补抽，供后续语料规模扩大后再评估。
    pageindex = PageIndexRetriever()
    pageindex.add(retriever.chunks)
    sources.register(pageindex, priority=120, enabled=False)
    # 实体抽取涉及思考型模型的重试，超时放宽到 120s。
    graph = GraphRetriever(llm, timeout_s=max(settings.node_timeout_s, 120.0))
    graph.add(retriever.chunks)
    sources.register(graph, priority=130, enabled=False)

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
        reranker=reranker,
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
        reranker=container.reranker,
        health=container.health,
        guardrail=container.guardrail,
        skills=container.skills,
        tools=container.tools,
        node_timeout_s=container.settings.node_timeout_s,
    )
