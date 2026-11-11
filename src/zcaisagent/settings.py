"""zCAISAgent 统一配置加载。

两层配置：
1. config.toml —— 模型提供者（base_url / key_env / reasoning_effort）、
   purpose → reasoning_effort 映射、默认 provider 等结构化内容。
2. .env / 进程环境变量 —— 只留扁平的 API key 与运行参数。
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

ENV_PREFIX = "ZCAISAGENT_"
ENV_FILE = "ZCAISAGENT_ENV_FILE"
CONFIG_FILE = "ZCAISAGENT_CONFIG_FILE"
_TRUE_VALUES = {"1", "true", "yes"}

Effort = Literal["low", "high", "max"]


def _load_env_file(path: Path) -> dict[str, str]:
    """读取 KEY=VALUE 形式的 .env，支持注释、引号与 export 前缀。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def _default_config_file() -> Path:
    override = os.getenv(CONFIG_FILE)
    if override:
        return Path(override).expanduser()
    if Path("config.toml").is_file():
        return Path("config.toml")
    return Path(__file__).resolve().parents[2] / "config.toml"


def _default_env_file() -> Path:
    override = os.getenv(ENV_FILE)
    if override:
        return Path(override).expanduser()
    if Path(".env").is_file():
        return Path(".env")
    return Path(__file__).resolve().parents[2] / ".env"


def _env_path(env: dict[str, str], name: str, default: str) -> Path:
    return Path(env.get(name, default)).expanduser()


# ---------------------------------------------------------------------------
# 新配置模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelProvider:
    """一个可直接调用的 LLM endpoint。"""

    id: str                  # provider id，用于引用和日志
    name: str                # 显示名
    model: str               # 传给 /chat/completions 的模型名
    base_url: str            # endpoint 前缀，不带尾部 `/`
    api_key: str             # 解析后的明文密钥（从 key_env 读）
    context_window: int
    reasoning_effort: Effort
    # 单价（$/1M tokens）；0 表示未配置，回退全局 input/output_price_per_mtok。
    cost_in: float = 0.0
    cost_out: float = 0.0


def _load_config_toml(path: Path) -> dict:
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _resolve_key(key_env: str, env: dict[str, str]) -> str:
    """从合并后的 env（进程环境变量 + .env）中取密钥；无值返回空串。"""
    return env.get(key_env, "")


@dataclass(frozen=True)
class EmbeddingConfig:
    """语义向量服务配置；None 表示未启用，vector 检索源不注册。"""

    model: str
    base_url: str
    api_key: str


@dataclass(frozen=True)
class DatabaseConfig:
    """pgvector/Postgres 持久化配置；None 表示未启用（纯内存）。"""

    enabled: bool
    dsn: str
    dimensions: int = 1024


@dataclass(frozen=True)
class RerankConfig:
    """专用 rerank 端点配置；网关不支持时容器自动回退语义余弦重排。

    base_url 为完整端点 URL；style 决定请求/响应格式：
    - "dashscope"：POST base_url，body {"model", "input": {"query", "documents"}}，
      响应 output.results[].relevance_score；
    - "cohere"：POST base_url，body {"model", "query", "documents"}，
      响应 results[].relevance_score（Jina/Cohere 兼容）。
    """

    model: str
    base_url: str
    api_key: str
    style: str = "dashscope"


@dataclass(frozen=True)
class SecurityConfig:
    """RBAC / 认证配置；未声明的角色默认无 scope。"""

    roles: dict[str, list[str]] = field(default_factory=dict)
    acl_tokens_by_role: dict[str, list[str]] = field(default_factory=dict)
    identity_mode: Literal["api_key", "oidc"] = "api_key"
    oidc_introspection_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret_env: str = ""
    oidc_timeout_s: float = 5.0
    # OPA 策略端点；为空时使用内置 RBAC。
    opa_url: str = ""
    opa_timeout_s: float = 3.0


@dataclass(frozen=True)
class ToolsConfig:
    """外部工具接入配置；默认只读、MCP 关闭。"""

    mcp_servers: list[dict] = field(default_factory=list)
    openapi: dict = field(default_factory=dict)
    sql: dict = field(default_factory=dict)
    enable_kb_search: bool = True
    enable_ticket: bool = False


@dataclass(frozen=True)
class MemoryConfig:
    """记忆子系统配置；默认开启，ttl_s=0 表示不过期。"""

    enabled: bool = True
    distill_enabled: bool = True
    max_items: int = 200
    ttl_s: float = 0.0


@dataclass(frozen=True)
class CacheConfig:
    """结果缓存配置；默认关闭，后端默认进程内。"""

    enabled: bool = False
    ttl_s: float = 300.0
    max_entries: int = 1000
    llm_enabled: bool = True
    backend: Literal["memory", "redis"] = "memory"
    redis_url: str = ""
    redis_timeout_s: float = 1.0


@dataclass(frozen=True)
class SchedulerConfig:
    """定时任务配置；默认关闭。"""

    enabled: bool = False
    tick_s: float = 30.0


@dataclass(frozen=True)
class EventsConfig:
    """事件投递配置；默认关闭。"""

    enabled: bool = False
    webhook_urls: list[str] = field(default_factory=list)
    secret_env: str = "ZCAISAGENT_EVENT_SECRET"
    max_retries: int = 3
    dead_letter_path: Path = field(
        default_factory=lambda: Path(".zcaisagent/dead_letter.jsonl")
    )


@dataclass(frozen=True)
class StudioConfig:
    """Studio 看板的数据文件路径：latest 报告、门禁阈值与基线回退目录。"""

    eval_report_path: Path = Path("data/eval/latest.json")
    eval_gate_path: Path = Path("configs/eval_gate.toml")
    baseline_dir: Path = Path("data/eval/baselines")


@dataclass(frozen=True)
class SecretConfig:
    """密钥来源配置；默认从环境变量读取。"""

    provider: Literal["env", "vault"] = "env"
    vault_addr: str = ""
    vault_token_env: str = "VAULT_TOKEN"
    vault_mount: str = "secret"
    vault_path: str = "zcaisagent"


@dataclass(frozen=True)
class ObservabilityConfig:
    """可观测配置：细粒度事件默认开启，OTel 导出默认关闭。"""

    emit_events: bool = True
    otel_enabled: bool = False
    otel_endpoint: str = ""
    service_name: str = "zcaisagent"
    sample_ratio: float = 1.0


@dataclass(frozen=True)
class GuardrailConfig:
    """护栏模型层配置：默认关闭，仅用规则；失败按 fail_open 放行。"""

    model_enabled: bool = False
    purpose: str = "guardrail"
    fail_open: bool = True


@dataclass(frozen=True)
class EvaluatorConfig:
    """语义校验（NLI judge）配置：默认关闭，回退词面校验。"""

    semantic_verify_enabled: bool = False
    purpose: str = "verify"
    min_entailment: float = 0.8
    fail_open: bool = True


@dataclass(frozen=True)
class SkillsConfig:
    """技能目录扫描配置：默认从 skills/ 加载。"""

    enabled: bool = True
    dir: Path = field(default_factory=lambda: Path("skills"))


@dataclass(frozen=True)
class CheckpointConfig:
    """检查点持久化配置：默认 JSONL + 压缩索引，可选 Postgres。"""

    backend: Literal["jsonl", "postgres"] = "jsonl"
    compact_enabled: bool = True
    compact_threshold_lines: int = 20
    compact_keep: int = 1


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if not value:
        return default
    return value.lower() in _TRUE_VALUES


def _env_list(name: str) -> list[str] | None:
    value = os.getenv(name)
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _build_providers(cfg: dict, env: dict[str, str]) -> dict[str, ModelProvider]:
    raw = cfg.get("model_providers", {}) or {}
    providers: dict[str, ModelProvider] = {}
    for pid, body in raw.items():
        providers[pid] = ModelProvider(
            id=pid,
            name=body.get("name", pid),
            model=body["model"],
            base_url=body["base_url"].rstrip("/"),
            api_key=_resolve_key(body.get("key_env", ""), env),
            context_window=int(body.get("context_window", 0)),
            reasoning_effort=body.get("reasoning_effort", "low"),
            cost_in=float(body.get("cost_in", 0) or 0),
            cost_out=float(body.get("cost_out", 0) or 0),
        )
    return providers


def _build_fallback_chain(
    providers: dict[str, ModelProvider],
    effort: Effort,
    effort_levels: list[Effort],
) -> list[ModelProvider]:
    """构造「就近双向」降级链：

    1. 先按 config.toml 里的定义顺序给出同等级全部 provider；
    2. 再从当前等级向两侧逐层扩展：low→high→max，max→high→low，high→low→max；
    3. 每一级按 config 顺序追加尚未出现过的 provider。
    """
    ordered = list(providers.values())

    def group_by(level: Effort) -> list[ModelProvider]:
        return [p for p in ordered if p.reasoning_effort == level]

    if effort not in effort_levels:
        effort = "low"

    idx = effort_levels.index(effort)
    # 双向扩展顺序：先近的一侧，再另一侧
    directions = []
    lo, hi = idx - 1, idx + 1
    while lo >= 0 or hi < len(effort_levels):
        if lo >= 0:
            directions.append(effort_levels[lo])
            lo -= 1
        if hi < len(effort_levels):
            directions.append(effort_levels[hi])
            hi += 1

    chain: list[ModelProvider] = []
    seen_ids: set[str] = set()

    def add(p: ModelProvider) -> None:
        if p.id not in seen_ids:
            seen_ids.add(p.id)
            chain.append(p)

    for p in group_by(effort):
        add(p)
    for neighbor_level in directions:
        for p in group_by(neighbor_level):
            add(p)
    return chain


@dataclass(frozen=True)
class Settings:
    """运行时配置。"""

    # —— LLM / Agent ——
    model_provider: str = ""
    model_providers: dict[str, ModelProvider] = field(default_factory=dict)
    purpose_effort: dict[str, Effort] = field(default_factory=dict)
    effort_levels: list[Effort] = field(default_factory=lambda: ["low", "high", "max"])
    llm_provider: str = "openai"
    # 语义向量检索；None 表示未启用。
    embedding: EmbeddingConfig | None = None
    # 专用 rerank 端点；None 表示未配置（回退语义余弦 / LLM 打分）。
    rerank: RerankConfig | None = None
    # rerank 总开关：无专用端点时控制回退重排器是否启用。
    rerank_enabled: bool = False
    # HyDE：开放问题用假设性答案扩展查询；默认关闭，避免额外模型调用。
    hyde_enabled: bool = False
    hyde_max_tokens: int = 256
    # 父子块：子块入库、父块随子块携带；默认关闭以保持既有切分口径。
    parent_child_enabled: bool = False
    parent_max_chars: int = 1800
    # 合成阶段证据正文的 token 预算（配合父块扩展防止上下文膨胀）。
    max_context_tokens: int = 6000
    # pgvector 持久化；None 表示未启用（纯内存，行为与旧版一致）。
    database: DatabaseConfig | None = None

    # —— M2 治理配置（默认 None / 关闭） ——
    security: SecurityConfig | None = None
    tools: ToolsConfig | None = None
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    cache: CacheConfig | None = None
    scheduler: SchedulerConfig | None = None
    events: EventsConfig | None = None
    secret: SecretConfig | None = None
    # Studio 看板读取的评估报告 / 门禁 / 基线路径。
    studio: StudioConfig = field(default_factory=StudioConfig)
    # 可观测 / 语义校验 / 技能目录 / 检查点。
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    guardrail: GuardrailConfig = field(default_factory=GuardrailConfig)
    evaluator: EvaluatorConfig = field(default_factory=EvaluatorConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    checkpoint: CheckpointConfig = field(default_factory=CheckpointConfig)

    # —— 运行参数 ——
    api_key: str = "dev-key"
    default_tenant: str = "demo"
    data_dir: Path = field(default_factory=lambda: Path(".zcaisagent"))
    seed_dir: Path = field(default_factory=lambda: Path("data/seed"))
    top_k: int = 5
    max_evidence_in_context: int = 8
    node_timeout_s: float = 30.0
    input_price_per_mtok: float = 0.0
    output_price_per_mtok: float = 0.0

    # —— 便捷方法 ——

    def resolve_effort(self, purpose: str) -> Effort:
        return self.purpose_effort.get(
            purpose, self.purpose_effort.get("default", "low")
        )

    def fallback_chain(self, effort: Effort) -> list[ModelProvider]:
        return _build_fallback_chain(self.model_providers, effort, self.effort_levels)

    def default_provider(self) -> ModelProvider | None:
        if self.model_provider:
            return self.model_providers.get(self.model_provider)
        return None

    @property
    def default_model_name(self) -> str:
        """当前默认 provider 的模型名，用于 trace 记录。

        无 provider 时（FakeLLM 场景）返回 "fake"。
        """
        provider = self.default_provider()
        return provider.model if provider else "fake"

    # —— 加载入口 ——

    @classmethod
    def from_env(
        cls,
        env_file: str | Path | None = None,
        config_file: str | Path | None = None,
    ) -> Settings:
        # 先读 .env，再让进程环境变量覆盖
        env_path = Path(env_file).expanduser() if env_file else _default_env_file()
        env = _load_env_file(env_path)
        env.update(os.environ)  # 进程环境变量全部覆盖 .env

        # config.toml
        cfg_path = (
            Path(config_file).expanduser() if config_file else _default_config_file()
        )
        cfg = _load_config_toml(cfg_path)

        providers = _build_providers(cfg, env)
        purpose_effort: dict[str, Effort] = dict(cfg.get("purpose_effort", {}) or {})
        effort_levels: list[Effort] = list(cfg.get("effort_levels") or ["low", "high", "max"])
        default_provider_id = cfg.get("model_provider", "")

        # 语义向量检索：config.toml [embedding] enabled=true 时启用；
        # 环境变量 ZCAISAGENT_EMBEDDING_ENABLED / ZCAISAGENT_RERANK_ENABLED 可覆盖开关。
        embedding: EmbeddingConfig | None = None
        raw_embedding = cfg.get("embedding", {}) or {}
        embed_on = bool(raw_embedding.get("enabled", False))
        if os.getenv(f"{ENV_PREFIX}EMBEDDING_ENABLED"):
            embed_on = os.getenv(f"{ENV_PREFIX}EMBEDDING_ENABLED", "").lower() in _TRUE_VALUES
        if embed_on:
            embedding = EmbeddingConfig(
                model=raw_embedding["model"],
                base_url=raw_embedding["base_url"].rstrip("/"),
                api_key=_resolve_key(raw_embedding.get("key_env", ""), env),
            )
        rerank_enabled = bool((cfg.get("rerank", {}) or {}).get("enabled", False))
        if os.getenv(f"{ENV_PREFIX}RERANK_ENABLED"):
            rerank_enabled = os.getenv(f"{ENV_PREFIX}RERANK_ENABLED", "").lower() in _TRUE_VALUES

        raw_retrieval = cfg.get("retrieval", {}) or {}
        hyde_enabled = bool(raw_retrieval.get("hyde_enabled", False))
        if os.getenv(f"{ENV_PREFIX}HYDE_ENABLED"):
            hyde_enabled = os.getenv(f"{ENV_PREFIX}HYDE_ENABLED", "").lower() in _TRUE_VALUES
        hyde_max_tokens = int(raw_retrieval.get("hyde_max_tokens", 256))
        parent_child_enabled = bool(raw_retrieval.get("parent_child_enabled", False))
        if os.getenv(f"{ENV_PREFIX}PARENT_CHILD_ENABLED"):
            parent_child_enabled = os.getenv(
                f"{ENV_PREFIX}PARENT_CHILD_ENABLED", ""
            ).lower() in {"1", "true", "yes"}
        parent_max_chars = int(raw_retrieval.get("parent_max_chars", 1800))
        max_context_tokens = int(raw_retrieval.get("max_context_tokens", 6000))

        # pgvector 持久化：连接失败由容器回退内存，这里只负责装配配置。
        database: DatabaseConfig | None = None
        raw_database = cfg.get("database", {}) or {}
        db_enabled = bool(raw_database.get("enabled", False))
        if os.getenv(f"{ENV_PREFIX}DATABASE_ENABLED"):
            db_enabled = os.getenv(f"{ENV_PREFIX}DATABASE_ENABLED", "").lower() in {
                "1",
                "true",
                "yes",
            }
        if db_enabled:
            dsn_env = str(raw_database.get("dsn_env", f"{ENV_PREFIX}DATABASE_URL"))
            database = DatabaseConfig(
                enabled=True,
                dsn=env.get(dsn_env, ""),
                dimensions=int(raw_database.get("dimensions", 1024)),
            )
        # [rerank] 段带 model/base_url 时视为专用 rerank 端点配置。
        rerank: RerankConfig | None = None
        raw_rerank = cfg.get("rerank", {}) or {}
        if rerank_enabled and raw_rerank.get("model") and raw_rerank.get("base_url"):
            rerank = RerankConfig(
                model=raw_rerank["model"],
                base_url=raw_rerank["base_url"],
                api_key=_resolve_key(raw_rerank.get("key_env", ""), env),
                style=str(raw_rerank.get("style", "dashscope")),
            )

        # [security]：角色 → scopes、ACL token 映射、身份模式与 OIDC introspection。
        raw_security = cfg.get("security")
        security: SecurityConfig | None = None
        if raw_security is not None:
            raw_oidc = raw_security.get("oidc", {}) or {}
            raw_opa = raw_security.get("opa", {}) or {}
            identity_mode = env.get(
                f"{ENV_PREFIX}SECURITY_IDENTITY",
                str(raw_security.get("identity", "api_key")),
            )
            security = SecurityConfig(
                roles={
                    role: list(scopes)
                    for role, scopes in (raw_security.get("roles", {}) or {}).items()
                },
                acl_tokens_by_role={
                    role: list(tokens)
                    for role, tokens in (
                        raw_security.get("acl_tokens_by_role", {}) or {}
                    ).items()
                },
                identity_mode="oidc" if identity_mode == "oidc" else "api_key",
                oidc_introspection_url=str(raw_oidc.get("introspection_url", "")),
                oidc_client_id=str(raw_oidc.get("client_id", "")),
                oidc_client_secret_env=str(raw_oidc.get("client_secret_env", "")),
                oidc_timeout_s=float(raw_oidc.get("timeout_s", 5.0)),
                opa_url=str(raw_opa.get("url", "")),
                opa_timeout_s=float(raw_opa.get("timeout_s", 3.0)),
            )

        # [tools]：MCP / OpenAPI / SQL 接入物 + 内置工具开关。
        raw_tools = cfg.get("tools")
        tools: ToolsConfig | None = None
        if raw_tools is not None:
            mcp_servers = [
                dict(server) for server in (raw_tools.get("mcp_servers", []) or [])
            ]
            nested_servers = (raw_tools.get("mcp", {}) or {}).get("servers", {}) or {}
            for name, body in nested_servers.items():
                mcp_servers.append({"name": name, **dict(body)})
            builtin = dict(raw_tools.get("builtin", {}) or {})
            tools = ToolsConfig(
                mcp_servers=mcp_servers,
                openapi=dict(raw_tools.get("openapi", {}) or {}),
                sql=dict(raw_tools.get("sql", {}) or {}),
                enable_kb_search=_env_flag(
                    f"{ENV_PREFIX}TOOLS_ENABLE_KB_SEARCH",
                    bool(raw_tools.get("enable_kb_search", True)),
                ),
                enable_ticket=_env_flag(
                    f"{ENV_PREFIX}TOOLS_ENABLE_TICKET",
                    bool(builtin.get("ticket_enabled", raw_tools.get("enable_ticket", False))),
                ),
            )

        # [memory]：三类记忆开关、条数上限与 TTL（0 表示不过期）。
        raw_memory = cfg.get("memory", {}) or {}
        memory = MemoryConfig(
            enabled=_env_flag(
                f"{ENV_PREFIX}MEMORY_ENABLED", bool(raw_memory.get("enabled", True))
            ),
            distill_enabled=_env_flag(
                f"{ENV_PREFIX}MEMORY_DISTILL_ENABLED",
                bool(raw_memory.get("distill_enabled", True)),
            ),
            max_items=int(
                env.get(f"{ENV_PREFIX}MEMORY_MAX_ITEMS", raw_memory.get("max_items", 200))
            ),
            ttl_s=float(env.get(f"{ENV_PREFIX}MEMORY_TTL_S", raw_memory.get("ttl_s", 0))),
        )

        raw_cache = cfg.get("cache")
        cache: CacheConfig | None = None
        if raw_cache is not None:
            backend = env.get(
                f"{ENV_PREFIX}CACHE_BACKEND", str(raw_cache.get("backend", "memory"))
            )
            cache = CacheConfig(
                enabled=_env_flag(
                    f"{ENV_PREFIX}CACHE_ENABLED", bool(raw_cache.get("enabled", False))
                ),
                ttl_s=float(env.get(f"{ENV_PREFIX}CACHE_TTL_S", raw_cache.get("ttl_s", 300))),
                max_entries=int(
                    env.get(
                        f"{ENV_PREFIX}CACHE_MAX_ENTRIES",
                        raw_cache.get("max_entries", 1000),
                    )
                ),
                llm_enabled=_env_flag(
                    f"{ENV_PREFIX}CACHE_LLM_ENABLED",
                    bool(raw_cache.get("llm_enabled", True)),
                ),
                backend="redis" if backend == "redis" else "memory",
                redis_url=env.get(
                    f"{ENV_PREFIX}REDIS_URL", str(raw_cache.get("redis_url", ""))
                ),
                redis_timeout_s=float(
                    env.get(
                        f"{ENV_PREFIX}REDIS_TIMEOUT_S",
                        raw_cache.get("redis_timeout_s", 1.0),
                    )
                ),
            )

        raw_scheduler = cfg.get("scheduler")
        scheduler: SchedulerConfig | None = None
        if raw_scheduler is not None:
            scheduler = SchedulerConfig(
                enabled=_env_flag(
                    f"{ENV_PREFIX}SCHEDULER_ENABLED",
                    bool(raw_scheduler.get("enabled", False)),
                ),
                tick_s=float(
                    env.get(f"{ENV_PREFIX}SCHEDULER_TICK_S", raw_scheduler.get("tick_s", 30))
                ),
            )

        raw_events = cfg.get("events")
        events: EventsConfig | None = None
        if raw_events is not None:
            webhook_urls = list(raw_events.get("webhook_urls", []) or [])
            override_urls = _env_list(f"{ENV_PREFIX}EVENTS_WEBHOOK_URLS")
            if override_urls is not None:
                webhook_urls = override_urls
            events = EventsConfig(
                enabled=_env_flag(
                    f"{ENV_PREFIX}EVENTS_ENABLED", bool(raw_events.get("enabled", False))
                ),
                webhook_urls=webhook_urls,
                secret_env=str(raw_events.get("secret_env", "ZCAISAGENT_EVENT_SECRET")),
                max_retries=int(raw_events.get("max_retries", 3)),
                dead_letter_path=Path(
                    env.get(
                        f"{ENV_PREFIX}EVENTS_DEAD_LETTER",
                        raw_events.get("dead_letter_path", ".zcaisagent/dead_letter.jsonl"),
                    )
                ).expanduser(),
            )

        raw_studio = cfg.get("studio", {}) or {}
        studio = StudioConfig(
            eval_report_path=_env_path(
                env,
                f"{ENV_PREFIX}STUDIO_EVAL_REPORT",
                str(raw_studio.get("eval_report_path", "data/eval/latest.json")),
            ),
            eval_gate_path=_env_path(
                env,
                f"{ENV_PREFIX}STUDIO_EVAL_GATE",
                str(raw_studio.get("eval_gate_path", "configs/eval_gate.toml")),
            ),
            baseline_dir=_env_path(
                env,
                f"{ENV_PREFIX}STUDIO_BASELINE_DIR",
                str(raw_studio.get("baseline_dir", "data/eval/baselines")),
            ),
        )

        raw_observability = cfg.get("observability", {}) or {}
        observability = ObservabilityConfig(
            emit_events=_env_flag(
                f"{ENV_PREFIX}OBS_EMIT_EVENTS",
                bool(raw_observability.get("emit_events", True)),
            ),
            otel_enabled=_env_flag(
                f"{ENV_PREFIX}OTEL_ENABLED",
                bool(raw_observability.get("otel_enabled", False)),
            ),
            otel_endpoint=env.get(
                f"{ENV_PREFIX}OTEL_ENDPOINT",
                str(raw_observability.get("otel_endpoint", "")),
            ),
            service_name=env.get(
                f"{ENV_PREFIX}OTEL_SERVICE",
                str(raw_observability.get("service_name", "zcaisagent")),
            ),
            sample_ratio=float(
                env.get(
                    f"{ENV_PREFIX}OTEL_SAMPLE_RATIO",
                    raw_observability.get("sample_ratio", 1.0),
                )
            ),
        )

        raw_guardrail = cfg.get("guardrail", {}) or {}
        guardrail = GuardrailConfig(
            model_enabled=_env_flag(
                f"{ENV_PREFIX}GUARDRAIL_MODEL_ENABLED",
                bool(raw_guardrail.get("model_enabled", False)),
            ),
            purpose=str(raw_guardrail.get("purpose", "guardrail")),
            fail_open=_env_flag(
                f"{ENV_PREFIX}GUARDRAIL_FAIL_OPEN",
                bool(raw_guardrail.get("fail_open", True)),
            ),
        )

        raw_evaluator = cfg.get("evaluator", {}) or {}
        evaluator = EvaluatorConfig(
            semantic_verify_enabled=_env_flag(
                f"{ENV_PREFIX}SEMANTIC_VERIFY_ENABLED",
                bool(raw_evaluator.get("semantic_verify_enabled", False)),
            ),
            purpose=str(raw_evaluator.get("purpose", "verify")),
            min_entailment=float(
                env.get(
                    f"{ENV_PREFIX}EVALUATOR_MIN_ENTAILMENT",
                    raw_evaluator.get("min_entailment", 0.8),
                )
            ),
            fail_open=_env_flag(
                f"{ENV_PREFIX}EVALUATOR_FAIL_OPEN",
                bool(raw_evaluator.get("fail_open", True)),
            ),
        )

        raw_skills = cfg.get("skills", {}) or {}
        skills = SkillsConfig(
            enabled=_env_flag(
                f"{ENV_PREFIX}SKILLS_ENABLED",
                bool(raw_skills.get("enabled", True)),
            ),
            dir=_env_path(
                env, f"{ENV_PREFIX}SKILLS_DIR", str(raw_skills.get("dir", "skills"))
            ),
        )

        raw_checkpoint = cfg.get("checkpoint", {}) or {}
        ckpt_backend = env.get(
            f"{ENV_PREFIX}CHECKPOINT_BACKEND",
            str(raw_checkpoint.get("backend", "jsonl")),
        )
        checkpoint = CheckpointConfig(
            backend="postgres" if ckpt_backend == "postgres" else "jsonl",
            compact_enabled=_env_flag(
                f"{ENV_PREFIX}CHECKPOINT_COMPACT",
                bool(raw_checkpoint.get("compact_enabled", True)),
            ),
            compact_threshold_lines=int(
                env.get(
                    f"{ENV_PREFIX}CHECKPOINT_THRESHOLD",
                    raw_checkpoint.get("compact_threshold_lines", 20),
                )
            ),
            compact_keep=int(
                env.get(
                    f"{ENV_PREFIX}CHECKPOINT_KEEP",
                    raw_checkpoint.get("compact_keep", 1),
                )
            ),
        )

        raw_secret = cfg.get("secret")
        secret: SecretConfig | None = None
        if raw_secret is not None:
            provider = env.get(
                f"{ENV_PREFIX}SECRET_PROVIDER", str(raw_secret.get("provider", "env"))
            )
            secret = SecretConfig(
                provider="vault" if provider == "vault" else "env",
                vault_addr=env.get(
                    f"{ENV_PREFIX}VAULT_ADDR", str(raw_secret.get("vault_addr", ""))
                ),
                vault_token_env=str(raw_secret.get("vault_token_env", "VAULT_TOKEN")),
                vault_mount=str(raw_secret.get("vault_mount", "secret")),
                vault_path=str(raw_secret.get("vault_path", "zcaisagent")),
            )

        return cls(
            model_provider=default_provider_id,
            model_providers=providers,
            purpose_effort=purpose_effort,
            effort_levels=effort_levels,
            embedding=embedding,
            rerank=rerank,
            rerank_enabled=rerank_enabled,
            hyde_enabled=hyde_enabled,
            hyde_max_tokens=hyde_max_tokens,
            parent_child_enabled=parent_child_enabled,
            parent_max_chars=parent_max_chars,
            max_context_tokens=max_context_tokens,
            database=database,
            security=security,
            tools=tools,
            memory=memory,
            cache=cache,
            scheduler=scheduler,
            events=events,
            secret=secret,
            studio=studio,
            observability=observability,
            guardrail=guardrail,
            evaluator=evaluator,
            skills=skills,
            checkpoint=checkpoint,
            llm_provider="openai" if providers else "fake",
            api_key=env.get(f"{ENV_PREFIX}API_KEY", "dev-key"),
            default_tenant=env.get(f"{ENV_PREFIX}DEFAULT_TENANT", "demo"),
            data_dir=_env_path(env, f"{ENV_PREFIX}DATA_DIR", ".zcaisagent"),
            seed_dir=_env_path(env, f"{ENV_PREFIX}SEED_DIR", "data/seed"),
            top_k=int(env.get(f"{ENV_PREFIX}TOP_K", "5")),
            max_evidence_in_context=int(
                env.get(f"{ENV_PREFIX}MAX_EVIDENCE", "8")
            ),
            node_timeout_s=float(env.get(f"{ENV_PREFIX}NODE_TIMEOUT_S", "30")),
            input_price_per_mtok=float(
                env.get(f"{ENV_PREFIX}INPUT_PRICE_PER_MTOK", "0")
            ),
            output_price_per_mtok=float(
                env.get(f"{ENV_PREFIX}OUTPUT_PRICE_PER_MTOK", "0")
            ),
        )
