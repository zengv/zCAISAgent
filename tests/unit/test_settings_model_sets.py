import os

from zcaisagent.settings import Settings


def _clear_env(monkeypatch) -> None:
    for key in list(os.environ):
        if key.startswith("ZCAISAGENT_") or key in {"AI_KEY_DS", "AI_KEY_MS", "AI_KEY_QW"}:
            monkeypatch.delenv(key)


def _write_config(tmp_path, body: str, *, env_body: str = ""):
    config_file = tmp_path / "config.toml"
    config_file.write_text(body, encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text(env_body, encoding="utf-8")
    return config_file, env_file


def test_default_provider_and_purpose_effort(tmp_path, monkeypatch) -> None:
    _clear_env(monkeypatch)
    _, env = _write_config(
        tmp_path,
        """
model_provider = "deepseek"
purpose_effort = { synthesize = "low", analyze = "high", default = "low" }

[model_providers.deepseek]
name = "Deepseek-flash"
model = "deepseek-flash"
base_url = "https://api.deepseek.com"
key_env = "AI_KEY_DS"
context_window = 128000
reasoning_effort = "high"
""",
        env_body="AI_KEY_DS=sk-demo",
    )
    settings = Settings.from_env(env_file=env, config_file=tmp_path / "config.toml")

    assert settings.model_provider == "deepseek"
    assert settings.default_provider().model == "deepseek-flash"
    assert settings.default_provider().api_key == "sk-demo"
    assert settings.resolve_effort("synthesize") == "low"
    assert settings.resolve_effort("analyze") == "high"
    assert settings.resolve_effort("never-seen-purpose") == "low"


def test_model_keys_prefer_env_file_over_process_env(tmp_path, monkeypatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("AI_KEY_DS", "from-process")
    _, env = _write_config(
        tmp_path,
        """
model_provider = "deepseek"

[model_providers.deepseek]
name = "DS"
model = "ds-flash"
base_url = "https://ds.example"
key_env = "AI_KEY_DS"
reasoning_effort = "high"
""",
        env_body="AI_KEY_DS=from-dotenv\n",
    )
    settings = Settings.from_env(env_file=env, config_file=tmp_path / "config.toml")
    assert settings.model_providers["deepseek"].api_key == "from-dotenv"


def test_model_key_falls_back_to_process_env(tmp_path, monkeypatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("AI_KEY_DS", "from-process")
    _, env = _write_config(
        tmp_path,
        """
model_provider = "deepseek"

[model_providers.deepseek]
name = "DS"
model = "ds-flash"
base_url = "https://ds.example"
key_env = "AI_KEY_DS"
reasoning_effort = "high"
""",
    )
    settings = Settings.from_env(env_file=env, config_file=tmp_path / "config.toml")
    assert settings.model_providers["deepseek"].api_key == "from-process"


def test_m2_sections_are_parsed(tmp_path, monkeypatch) -> None:
    _clear_env(monkeypatch)
    _, env = _write_config(
        tmp_path,
        """
[security]
identity = "oidc"
roles = { support = ["retrieve", "tool:read"] }
acl_tokens_by_role = { support = ["public"] }

[security.oidc]
introspection_url = "https://idp.test/introspect"
client_id = "zcais"
client_secret_env = "OIDC_SECRET"

[security.opa]
url = "https://opa.test/v1/data/zcais/allow"

[tools]
enable_kb_search = false
enable_ticket = true
mcp_servers = [{ name = "local", command = "python", enabled = false }]

[tools.openapi.sources.api]
enabled = false

[tools.sql.sources.support]
path = "demo.db"
tables = ["tickets"]

[memory]
enabled = true
distill_enabled = false
max_items = 50
ttl_s = 60

[cache]
enabled = true
ttl_s = 120
max_entries = 5
llm_enabled = false

[scheduler]
enabled = true
tick_s = 5

[events]
enabled = true
webhook_urls = ["https://example.test/hook"]
secret_env = "HOOK_SECRET"
max_retries = 4
dead_letter_path = "tmp/dead.jsonl"

[secret]
provider = "vault"
vault_addr = "https://vault.test"
vault_token_env = "VT"
vault_mount = "kv"
vault_path = "zcaisagent"
""",
        env_body="",
    )
    settings = Settings.from_env(env_file=env, config_file=tmp_path / "config.toml")

    assert settings.security is not None
    assert settings.security.identity_mode == "oidc"
    assert settings.security.roles == {"support": ["retrieve", "tool:read"]}
    assert settings.security.acl_tokens_by_role == {"support": ["public"]}
    assert settings.security.oidc_introspection_url == "https://idp.test/introspect"
    assert settings.security.opa_url == "https://opa.test/v1/data/zcais/allow"

    assert settings.tools is not None
    assert settings.tools.enable_kb_search is False
    assert settings.tools.enable_ticket is True
    assert settings.tools.mcp_servers == [
        {"name": "local", "command": "python", "enabled": False}
    ]
    assert set(settings.tools.openapi["sources"]) == {"api"}
    assert set(settings.tools.sql["sources"]) == {"support"}

    assert settings.memory.max_items == 50
    assert settings.memory.distill_enabled is False
    assert settings.memory.ttl_s == 60.0

    assert settings.cache is not None
    assert settings.cache.enabled is True
    assert settings.cache.llm_enabled is False
    assert settings.cache.max_entries == 5

    assert settings.scheduler is not None
    assert settings.scheduler.enabled is True
    assert settings.scheduler.tick_s == 5.0

    assert settings.events is not None
    assert settings.events.webhook_urls == ["https://example.test/hook"]
    assert settings.events.secret_env == "HOOK_SECRET"
    assert settings.events.max_retries == 4
    assert settings.events.dead_letter_path.name == "dead.jsonl"

    assert settings.secret is not None
    assert settings.secret.provider == "vault"
    assert settings.secret.vault_addr == "https://vault.test"
    assert settings.secret.vault_mount == "kv"


def test_fallback_chain_same_then_neighbor_bidirectional(tmp_path, monkeypatch) -> None:
    _clear_env(monkeypatch)
    _, env = _write_config(
        tmp_path,
        """
model_provider = "deepseek"
purpose_effort = { default = "low" }
effort_levels = ["low", "high", "max"]

[model_providers.modelscope-ds-flash]
name = "MS-flash"
model = "ms-ds-flash"
base_url = "https://ms.example/v1"
key_env = "AI_KEY_MS"
context_window = 128000
reasoning_effort = "low"

[model_providers.modelscope-glm-flash]
name = "GLM-flash"
model = "ms-glm-flash"
base_url = "https://ms.example/v1"
key_env = "AI_KEY_MS"
context_window = 128000
reasoning_effort = "low"

[model_providers.deepseek]
name = "DS-flash"
model = "ds-flash"
base_url = "https://ds.example"
key_env = "AI_KEY_DS"
context_window = 128000
reasoning_effort = "high"

[model_providers.qwen]
name = "QW-flash"
model = "qw-flash"
base_url = "https://qw.example"
key_env = "AI_KEY_QW"
context_window = 128000
reasoning_effort = "high"

[model_providers.deepseek-pro]
name = "DS-pro"
model = "ds-pro"
base_url = "https://ds.example"
key_env = "AI_KEY_DS"
context_window = 128000
reasoning_effort = "max"

[model_providers.qwen-max]
name = "QW-max"
model = "qw-max"
base_url = "https://qw.example"
key_env = "AI_KEY_QW"
context_window = 128000
reasoning_effort = "max"
""",
        env_body="AI_KEY_MS=ms\nAI_KEY_DS=ds\nAI_KEY_QW=qw",
    )
    settings = Settings.from_env(env_file=env, config_file=tmp_path / "config.toml")

    # 请求 low：先 low 全部(配置顺序) → 就近 high → 就近 max
    low_chain = [p.id for p in settings.fallback_chain("low")]
    assert low_chain == [
        "modelscope-ds-flash",
        "modelscope-glm-flash",
        "deepseek",
        "qwen",
        "deepseek-pro",
        "qwen-max",
    ]

    # 请求 high：先 high 全部 → 就近 low → 就近 max
    high_chain = [p.id for p in settings.fallback_chain("high")]
    assert high_chain == [
        "deepseek",
        "qwen",
        "modelscope-ds-flash",
        "modelscope-glm-flash",
        "deepseek-pro",
        "qwen-max",
    ]

    # 请求 max：先 max 全部 → 就近 high → 就近 low
    max_chain = [p.id for p in settings.fallback_chain("max")]
    assert max_chain == [
        "deepseek-pro",
        "qwen-max",
        "deepseek",
        "qwen",
        "modelscope-ds-flash",
        "modelscope-glm-flash",
    ]
