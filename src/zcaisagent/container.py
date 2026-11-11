import asyncio
import hashlib
import json
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from zcaisagent.adapters.cache.llm import CachedLLM
from zcaisagent.adapters.cache.memory import InMemoryCache
from zcaisagent.adapters.cache.redis import RedisCache
from zcaisagent.adapters.checkpoint.jsonl import JsonlCheckpoint
from zcaisagent.adapters.events.webhook import WebhookEventSink
from zcaisagent.adapters.health.memory import HealthBoard
from zcaisagent.adapters.llm.fake import FakeLLM
from zcaisagent.adapters.llm.openai_compat import OpenAICompatEmbedding, OpenAICompatLLM
from zcaisagent.adapters.secret.vault import VaultSecret
from zcaisagent.adapters.security.oidc import OidcIdentity
from zcaisagent.adapters.security.opa import OpaPolicy
from zcaisagent.adapters.tools.openapi import build_openapi_tools
from zcaisagent.adapters.trace.jsonl import JsonlTrace
from zcaisagent.capabilities.memory.store import InMemoryMemory
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
from zcaisagent.capabilities.tools.builtin import (
    KnowledgeBaseSearchTool,
    TicketCreateTool,
    TicketListTool,
    TicketStore,
)
from zcaisagent.capabilities.tools.gateway import ToolGateway
from zcaisagent.capabilities.tools.mcp_client import discover_mcp_tools
from zcaisagent.capabilities.tools.sql_tool import SqlReadonlyTool
from zcaisagent.domain.enums import AutonomyLevel
from zcaisagent.domain.mandate import Mandate
from zcaisagent.domain.models import Chunk, Message, RunState
from zcaisagent.governance.audit import EnvSecret, JsonlAudit
from zcaisagent.governance.guards import RuleGuardrail
from zcaisagent.governance.identity import ApiKeyIdentity, RbacPolicy
from zcaisagent.ingestion.pipeline import ingest_directory
from zcaisagent.orchestration.policies.budget import BudgetPolicy
from zcaisagent.orchestration.runtime import RunContext
from zcaisagent.ports.cache import CachePort
from zcaisagent.ports.checkpoint import CheckpointPort
from zcaisagent.ports.events import EventSinkPort
from zcaisagent.ports.health import HealthStatus
from zcaisagent.ports.llm import LLMPort
from zcaisagent.ports.retrieval import RetrieverPort
from zcaisagent.ports.security import (
    AuditPort,
    AuthContext,
    IdentityPort,
    PolicyPort,
    SecretPort,
)
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
    identity: IdentityPort
    policy: PolicyPort
    guardrail: RuleGuardrail
    secret: SecretPort
    health: HealthBoard
    skills: InMemorySkillRegistry
    tools: ToolGateway
    memory: InMemoryMemory
    # 融合后 LLM 相关性重排；None 表示关闭（回退词法相关性逻辑）。
    reranker: LLMPointwiseReranker | None = None
    # 结果缓存；None 表示 [cache] 未启用。
    cache: CachePort | None = None
    # Webhook 事件出口；None 表示 [events] 未启用。
    events: EventSinkPort | None = None
    # 索引版本摘要，参与检索缓存键；空索引为 "empty"。
    index_version: str = "empty"


def build_cache(
    settings: Settings,
    *,
    client_factory: Callable[[str, float], Any] | None = None,
) -> CachePort | None:
    """按 [cache] 配置装配缓存；未启用返回 None。

    Redis 后端始终带 InMemoryCache 回退；客户端创建失败（依赖缺失或工厂
    异常）时直接退回进程内缓存。
    """
    config = settings.cache
    if config is None or not config.enabled:
        return None
    memory = InMemoryCache(ttl_s=config.ttl_s, max_entries=config.max_entries)
    if config.backend != "redis" or not config.redis_url:
        return memory
    client = None
    if client_factory is not None:
        try:
            client = client_factory(config.redis_url, config.redis_timeout_s)
        except Exception:  # noqa: BLE001 - Redis 可选，创建失败回退内存
            return memory
    return RedisCache(
        config.redis_url,
        ttl_s=config.ttl_s,
        timeout_s=config.redis_timeout_s,
        client=client,
        fallback=memory,
    )


async def invalidate_retrieval_cache(cache: CachePort | None) -> None:
    """索引变化后清除全部检索缓存；cache 为 None 时无操作。"""
    if cache is None:
        return
    await cache.delete_prefix("retrieval:")


def _invalidate_retrieval_cache_sync(cache: CachePort | None) -> None:
    """装配期清理检索缓存；已有事件循环时无法同步等待（新实例本为空）。"""
    if cache is None:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(invalidate_retrieval_cache(cache))


def index_version_of(chunks: list[Chunk]) -> str:
    """对 (id, version) 排序后取 sha256 前 12 位，标识当前索引版本。"""
    if not chunks:
        return "empty"
    payload = sorted((chunk.id, chunk.version or "") for chunk in chunks)
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return digest[:12]


def _tool_sources(raw: dict) -> dict[str, dict]:
    """兼容 ``{"sources": {name: {...}}}`` 与单源平铺两种写法。"""
    if not isinstance(raw, dict) or not raw:
        return {}
    sources = raw.get("sources")
    if isinstance(sources, dict):
        return {
            str(name): dict(body)
            for name, body in sources.items()
            if isinstance(body, dict)
        }
    return {"default": dict(raw)}


def _secret_values(settings: Settings) -> dict[str, str]:
    values = {"api_key": settings.api_key}
    if settings.tools is not None:
        for source in _tool_sources(settings.tools.openapi).values():
            key = str(source.get("secret_key") or "")
            if key:
                values.setdefault(key, os.getenv(key, ""))
    return values


def _report_health(
    health: HealthBoard | None,
    name: str,
    ok: bool,
    message: str = "",
    owner: str = "tools",
) -> None:
    if health is None:
        return
    try:
        health.register(name, owner)
    except ValueError:
        pass
    health.report(name, HealthStatus.OK if ok else HealthStatus.DOWN, message)


def build_secret(settings: Settings, health: HealthBoard | None = None) -> SecretPort:
    """按 ``[secret]`` 装配密钥源；Vault 配置不完整时回退 env 并标记降级。"""
    env_secret = EnvSecret(_secret_values(settings))
    config = settings.secret
    if config is None or config.provider != "vault" or not config.vault_addr:
        _report_health(health, "secret", True, "env", owner="secret")
        return env_secret
    token = os.getenv(config.vault_token_env, "") if config.vault_token_env else ""
    vault = VaultSecret(
        config.vault_addr,
        token,
        mount=config.vault_mount,
        path=config.vault_path,
        fallback=env_secret,
    )
    if token:
        _report_health(health, "secret", True, "vault", owner="secret")
    else:
        _report_health(
            health,
            "secret",
            False,
            "vault token missing, fallback env",
            owner="secret",
        )
    return vault


def build_events(
    settings: Settings, health: HealthBoard | None = None
) -> EventSinkPort | None:
    """按 ``[events]`` 装配 Webhook 投递；默认关闭时返回 None。"""
    config = settings.events
    if config is None or not config.enabled or not config.webhook_urls:
        _report_health(health, "events", True, "disabled", owner="events")
        return None
    secret = os.getenv(config.secret_env, "") if config.secret_env else ""
    sink = WebhookEventSink(
        list(config.webhook_urls),
        secret,
        max_retries=config.max_retries,
        dead_letter_path=config.dead_letter_path,
    )
    _report_health(
        health,
        "events",
        True,
        f"{len(config.webhook_urls)} webhook(s)",
        owner="events",
    )
    return sink


def build_configured_tools(
    settings: Settings,
    *,
    audit: AuditPort | None = None,
    retriever: RetrieverPort | None = None,
    secret: SecretPort | None = None,
    health: HealthBoard | None = None,
) -> ToolGateway:
    """按 ``[tools]`` 配置注册内置 / MCP / OpenAPI / SQL 工具。

    外部源加载失败只写健康项降级，不阻断容器启动；MCP 需要启动子进程，
    在已有事件循环中会退化为健康项降级（由调用方改用异步装配）。
    """
    gateway = ToolGateway(audit=audit)
    tools = settings.tools
    if tools is None:
        if retriever is not None:
            gateway.register(KnowledgeBaseSearchTool(retriever))
        return gateway

    if tools.enable_kb_search and retriever is not None:
        gateway.register(KnowledgeBaseSearchTool(retriever))

    if tools.enable_ticket:
        store = TicketStore()
        gateway.register(TicketCreateTool(store))
        gateway.register(TicketListTool(store))

    for server in tools.mcp_servers:
        name = str(server.get("name") or "mcp")
        health_name = f"tools.mcp.{name}"
        if not server.get("enabled", True):
            _report_health(health, health_name, True, "disabled")
            continue
        try:
            adapters = asyncio.run(discover_mcp_tools(server))
            for adapter in adapters:
                gateway.register(adapter)
        except Exception as exc:  # noqa: BLE001 - 外部工具失败不阻断启动
            _report_health(health, health_name, False, str(exc))
        else:
            _report_health(health, health_name, True)

    for name, source in _tool_sources(tools.openapi).items():
        health_name = f"tools.openapi.{name}"
        if not source.get("enabled", True):
            _report_health(health, health_name, True, "disabled")
            continue
        try:
            openapi_tools = build_openapi_tools(source, secret=secret)
            for tool in openapi_tools:
                gateway.register(tool)
        except Exception as exc:  # noqa: BLE001 - 外部工具失败不阻断启动
            _report_health(health, health_name, False, str(exc))
        else:
            _report_health(health, health_name, True)

    for name, source in _tool_sources(tools.sql).items():
        health_name = f"tools.sql.{name}"
        if not source.get("enabled", True):
            _report_health(health, health_name, True, "disabled")
            continue
        try:
            db_path = str(source.get("path") or "")
            tables = [str(item) for item in source.get("tables") or []]
            if not db_path or not tables:
                raise ValueError("sql source requires path and tables")
            gateway.register(
                SqlReadonlyTool(
                    db_path,
                    tables,
                    max_rows=int(source.get("max_rows", 100)),
                    name=str(source.get("tool_name") or "sql_readonly"),
                )
            )
        except Exception as exc:  # noqa: BLE001 - 外部工具失败不阻断启动
            _report_health(health, health_name, False, str(exc))
        else:
            _report_health(health, health_name, True)

    return gateway


def _build_security(
    settings: Settings, health: HealthBoard | None = None
) -> tuple[IdentityPort, PolicyPort]:
    """按 [security] 装配身份与策略；OIDC 配置缺失时回退 API Key 并标记降级。"""
    security = settings.security
    identity: IdentityPort = ApiKeyIdentity(
        settings.api_key, settings.default_tenant, security
    )
    if security is not None and security.identity_mode == "oidc":
        client_secret = (
            os.getenv(security.oidc_client_secret_env, "")
            if security.oidc_client_secret_env
            else ""
        )
        if (
            security.oidc_introspection_url
            and security.oidc_client_id
            and client_secret
        ):
            identity = OidcIdentity(
                security.oidc_introspection_url,
                security.oidc_client_id,
                client_secret,
                timeout_s=security.oidc_timeout_s,
                security=security,
            )
            _report_health(
                health, "security.identity", True, "oidc", owner="security"
            )
        else:
            _report_health(
                health,
                "security.identity",
                False,
                "oidc config incomplete, fallback api_key",
                owner="security",
            )
    else:
        _report_health(health, "security.identity", True, "api_key", owner="security")

    policy: PolicyPort = RbacPolicy(security)
    if security is not None and security.opa_url:
        policy = OpaPolicy(security.opa_url, timeout_s=security.opa_timeout_s)
        _report_health(health, "security.policy", True, "opa", owner="security")
    else:
        _report_health(health, "security.policy", True, "rbac", owner="security")
    return identity, policy


def build_container(settings: Settings | None = None, ingest: bool = True) -> Container:
    settings = settings or Settings.from_env()
    cache = build_cache(settings)
    _invalidate_retrieval_cache_sync(cache)
    health = HealthBoard()
    for name in ("runtime", "llm", "retrieval", "checkpoint", "trace"):
        health.register(name, name)
        health.report(name, HealthStatus.OK)
    health.register("cache", "cache")
    health.report(
        "cache",
        HealthStatus.OK,
        settings.cache.backend if cache is not None and settings.cache else "disabled",
    )

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
    index_version = index_version_of(retriever.chunks)
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
    # LLM 结果缓存：仅缓存 temperature=0 的非流式请求。
    if cache is not None and settings.cache is not None and settings.cache.llm_enabled:
        llm = CachedLLM(llm, cache, ttl_s=settings.cache.ttl_s)
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
    secret = build_secret(settings, health)
    events = build_events(settings, health)
    identity, policy = _build_security(settings, health)

    return Container(
        settings=settings,
        retriever=retriever,
        sources=sources,
        llm=llm,
        checkpoint=JsonlCheckpoint(settings.data_dir / "checkpoints"),
        trace=JsonlTrace(settings.data_dir / "trace.jsonl"),
        audit=audit,
        identity=identity,
        policy=policy,
        guardrail=RuleGuardrail(),
        secret=secret,
        health=health,
        skills=build_default_skill_registry(),
        memory=InMemoryMemory(
            max_items=settings.memory.max_items,
            ttl_s=settings.memory.ttl_s,
        ),
        tools=build_configured_tools(
            settings,
            audit=audit,
            retriever=retriever,
            secret=secret,
            health=health,
        ),
        reranker=reranker,
        cache=cache,
        events=events,
        index_version=index_version,
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
        audit=container.audit,
        skills=container.skills,
        tools=container.tools,
        memory=container.memory,
        cache=container.cache,
        events=container.events,
        node_timeout_s=container.settings.node_timeout_s,
        extra={"index_version": container.index_version},
    )
