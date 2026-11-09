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
    ) -> "Settings":
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

        return cls(
            model_provider=default_provider_id,
            model_providers=providers,
            purpose_effort=purpose_effort,
            effort_levels=effort_levels,
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
