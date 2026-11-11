"""配置契约测试：config.toml、Settings 解析与 docs/03 §17 文档三者保持一致。"""

import tomllib
from pathlib import Path

from zcaisagent.settings import Settings

ROOT = Path(__file__).resolve().parents[2]

# Settings.from_env 会读取的顶层键（等见 settings.py 的 cfg.get 调用）。
SUPPORTED_TOPLEVEL = {
    "model_provider",
    "effort_levels",
    "purpose_effort",
    "model_providers",
    "embedding",
    "rerank",
    "retrieval",
    "database",
    "security",
    "tools",
    "memory",
    "cache",
    "scheduler",
    "events",
    "secret",
    "studio",
    "observability",
    "guardrail",
    "evaluator",
    "skills",
    "checkpoint",
}

# docs/03 §17 必须覆盖到的 section 表。
SECTION_TABLES = {
    "purpose_effort",
    "model_providers",
    "embedding",
    "rerank",
    "retrieval",
    "database",
    "security",
    "tools",
    "memory",
    "cache",
    "scheduler",
    "events",
    "secret",
    "studio",
    "observability",
    "guardrail",
    "evaluator",
    "skills",
    "checkpoint",
}


def test_repo_config_only_uses_supported_sections() -> None:
    cfg = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
    unknown = set(cfg) - SUPPORTED_TOPLEVEL
    assert not unknown, f"config.toml 出现未支持的顶层键: {sorted(unknown)}"


def test_docs17_documents_all_supported_sections() -> None:
    text = (ROOT / "docs" / "03-architecture.md").read_text(encoding="utf-8")
    start = text.index("## 17. 配置契约")
    end = text.index("## 18.", start)
    section = text[start:end]
    missing = [
        name
        for name in SECTION_TABLES
        if f"[{name}]" not in section and f"[{name}." not in section
    ]
    assert not missing, f"docs/03 §17 缺少 section: {sorted(missing)}"
    for scalar_key in ("model_provider", "effort_levels"):
        assert scalar_key in section, f"docs/03 §17 缺少顶层键: {scalar_key}"


def test_settings_loads_repo_config(tmp_path) -> None:
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    settings = Settings.from_env(env_file=empty_env, config_file=ROOT / "config.toml")
    assert settings.model_providers, "仓库 config.toml 应至少定义一个 provider"
    assert settings.top_k == 5
