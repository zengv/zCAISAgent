import os

from tests.conftest import make_state
from zcaisagent.domain.enums import ApprovalStatus, MemoryKind
from zcaisagent.domain.models import PendingApproval, ToolInvocation
from zcaisagent.domain.patch import StatePatch
from zcaisagent.orchestration.reducers import apply_patch
from zcaisagent.ports.cache import CacheStats
from zcaisagent.ports.events import EventEnvelope
from zcaisagent.ports.memory import MemoryItem, MemorySource, MemoryWrite
from zcaisagent.ports.scheduler import JobSpec
from zcaisagent.ports.tools import ToolResult
from zcaisagent.settings import CacheConfig, SecurityConfig, Settings


def test_pending_approval_defaults() -> None:
    approval = PendingApproval()
    assert approval.id == ""
    assert approval.args == {}
    assert approval.risk_level == "write"
    assert approval.status is ApprovalStatus.PENDING
    assert approval.decided_at is None
    assert approval.decided_by is None
    assert approval.requested_at.tzinfo is not None


def test_tool_invocation_defaults() -> None:
    invocation = ToolInvocation(id="inv_1", tool="kb_search")
    assert invocation.status == "ok"
    assert invocation.risk_level == "read"
    assert invocation.approval_id is None


def test_tool_result_new_fields_are_backward_compatible() -> None:
    result = ToolResult(tool="kb_search", ok=True, output="ok")
    assert result.approval_required is False
    assert result.approval_id is None
    assert result.quarantined is False


def test_reducer_applies_and_clears_pending_approval() -> None:
    state = make_state("测试")
    approval = PendingApproval(
        id="apv_1", run_id=state.run_id, tool="ticket_create", args={"title": "t"}
    )
    state = apply_patch(state, StatePatch(pending_approval=approval, resume_node="act"))
    assert state.pending_approval is not None
    assert state.pending_approval.id == "apv_1"
    assert state.pending_approval.args == {"title": "t"}
    assert state.resume_node == "act"

    state = apply_patch(state, StatePatch(clear_pending_approval=True))
    assert state.pending_approval is None
    assert state.resume_node == "act"


def test_cache_stats_hit_rate() -> None:
    assert CacheStats(hits=3, misses=1).hit_rate == 0.75
    assert CacheStats(hits=0, misses=0).hit_rate == 0.0


def test_job_spec_defaults() -> None:
    job = JobSpec(job_id="job_1", tenant_id="demo", name="日报", cron="0 8 * * 1-5")
    assert job.timezone == "Asia/Shanghai"
    assert job.enabled is True
    assert job.concurrency == "forbid"
    assert job.misfire == "run_once"
    assert job.timeout_s == 300
    assert job.budget_profile == "scheduled"
    assert job.notify == []
    assert job.schema_version == 1


def test_event_envelope_defaults() -> None:
    event = EventEnvelope(
        type="run.finished", tenant_id="demo", run_id="run_1", data={"ok": True}
    )
    assert len(event.id) == 32
    assert event.ts > 0
    assert event.data == {"ok": True}


def test_memory_write_defaults() -> None:
    write = MemoryWrite(item=MemoryItem(id="m1", kind=MemoryKind.SEMANTIC, content="偏好"))
    assert write.source is MemorySource.USER
    assert write.explicit is False
    assert write.risk == "low"


def test_settings_new_configs_default_off() -> None:
    settings = Settings()
    assert settings.security is None
    assert settings.tools is None
    assert settings.cache is None
    assert settings.scheduler is None
    assert settings.events is None
    assert settings.secret is None
    assert CacheConfig().enabled is False
    assert SecurityConfig().identity_mode == "api_key"


def test_from_env_reads_security_and_cache(tmp_path, monkeypatch) -> None:
    for key in list(os.environ):
        if key.startswith("ZCAISAGENT_"):
            monkeypatch.delenv(key)
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[cache]
enabled = true
ttl_s = 60
max_entries = 8
llm_enabled = true

[security]
identity = "oidc"

[security.roles]
support = ["retrieve", "tools:read"]

[security.acl_tokens_by_role]
support = ["public"]

[security.oidc]
introspection_url = "https://idp.example/introspect"
client_id = "zcais"
client_secret_env = "OIDC_SECRET"
""",
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")

    settings = Settings.from_env(env_file=env_file, config_file=config_file)

    assert settings.cache is not None
    assert settings.cache.enabled is True
    assert settings.cache.ttl_s == 60
    assert settings.cache.max_entries == 8
    assert settings.cache.llm_enabled is True
    assert settings.security is not None
    assert settings.security.roles == {"support": ["retrieve", "tools:read"]}
    assert settings.security.acl_tokens_by_role == {"support": ["public"]}
    assert settings.security.identity_mode == "oidc"
    assert settings.security.oidc_introspection_url == "https://idp.example/introspect"
    assert settings.security.oidc_client_secret_env == "OIDC_SECRET"
    assert settings.scheduler is None
    assert settings.events is None
