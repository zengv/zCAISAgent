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
