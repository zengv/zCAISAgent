import asyncio

import pytest

from zcaisagent.capabilities.tools.gateway import (
    ToolGateway,
    build_default_tool_gateway,
    to_evidence,
    validate_args,
)
from zcaisagent.domain.enums import SourceType
from zcaisagent.ports.security import AuditEvent, AuthContext
from zcaisagent.ports.tools import ToolResult, ToolSpec

_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}},
    "required": ["query"],
    "additionalProperties": False,
}


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def append(self, event: AuditEvent) -> None:
        self.events.append(event)


class FakeTool:
    spec: ToolSpec

    def __init__(
        self,
        spec: ToolSpec,
        output: object = "ok",
        delay: float = 0.0,
        exc: Exception | None = None,
    ) -> None:
        self.spec = spec
        self._output = output
        self._delay = delay
        self._exc = exc

    async def invoke(self, args: dict, ctx: AuthContext) -> ToolResult:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._exc is not None:
            raise self._exc
        return ToolResult(tool=self.spec.name, ok=True, output=self._output, latency_ms=7)


def _spec(**overrides: object) -> ToolSpec:
    base: dict = {
        "name": "kb_search",
        "description": "search knowledge base",
        "input_schema": _SCHEMA,
        "scopes": ["tools:read"],
        "timeout_s": 5,
    }
    base.update(overrides)
    return ToolSpec(**base)


def _auth(scopes: list[str] | None = None) -> AuthContext:
    granted = ["tools:read"] if scopes is None else scopes
    return AuthContext(tenant_id="demo", user_id="tester", scopes=granted)


def _gateway(tool: FakeTool) -> tuple[ToolGateway, FakeAudit]:
    audit = FakeAudit()
    gateway = ToolGateway(audit=audit)
    gateway.register(tool)
    return gateway, audit


def test_register_rejects_duplicate_name() -> None:
    tool = FakeTool(_spec())
    gateway = ToolGateway()
    gateway.register(tool)
    with pytest.raises(ValueError):
        gateway.register(tool)


def test_default_gateway_starts_empty() -> None:
    gateway = build_default_tool_gateway()
    assert gateway.all() == []


def test_unregistered_tool_is_rejected_and_audited() -> None:
    gateway, audit = _gateway(FakeTool(_spec()))
    result = asyncio.run(gateway.call("missing", {}, _auth(), run_id="run_1"))
    assert not result.ok
    assert result.error == "TOOL_NOT_FOUND"
    assert audit.events[-1].detail["outcome"] == "TOOL_NOT_FOUND"
    assert audit.events[-1].run_id == "run_1"


def test_write_tool_requires_approval() -> None:
    gateway, audit = _gateway(FakeTool(_spec(risk_level="write")))
    result = asyncio.run(gateway.call("kb_search", {"query": "x"}, _auth()))
    assert not result.ok
    assert result.error == "TOOL_REQUIRES_APPROVAL"
    assert audit.events[-1].detail["outcome"] == "TOOL_REQUIRES_APPROVAL"


def test_read_tool_with_approval_flag_is_rejected() -> None:
    gateway, _ = _gateway(FakeTool(_spec(requires_approval=True)))
    result = asyncio.run(gateway.call("kb_search", {"query": "x"}, _auth()))
    assert not result.ok
    assert result.error == "TOOL_REQUIRES_APPROVAL"


def test_missing_scope_denies_without_invoking() -> None:
    gateway, audit = _gateway(FakeTool(_spec()))
    result = asyncio.run(gateway.call("kb_search", {"query": "x"}, _auth([])))
    assert not result.ok
    assert result.error == "TOOL_UNAUTHORIZED"
    assert audit.events[-1].detail["missing"] == ["tools:read"]


def test_missing_required_argument_is_rejected() -> None:
    gateway, _ = _gateway(FakeTool(_spec()))
    result = asyncio.run(gateway.call("kb_search", {}, _auth()))
    assert not result.ok
    assert result.error == "TOOL_INVALID_ARGS"


def test_undeclared_argument_is_rejected() -> None:
    gateway, _ = _gateway(FakeTool(_spec()))
    result = asyncio.run(gateway.call("kb_search", {"query": "x", "extra": 1}, _auth()))
    assert not result.ok
    assert result.error == "TOOL_INVALID_ARGS"


def test_wrong_argument_type_is_rejected() -> None:
    gateway, _ = _gateway(FakeTool(_spec()))
    result = asyncio.run(gateway.call("kb_search", {"query": 1}, _auth()))
    assert not result.ok
    assert result.error == "TOOL_INVALID_ARGS"


def test_validate_args_rejects_bool_as_integer() -> None:
    errors = validate_args({"properties": {"top_k": {"type": "integer"}}}, {"top_k": True})
    assert errors == ["type:top_k"]


def test_successful_call_truncates_long_output() -> None:
    gateway, audit = _gateway(
        FakeTool(_spec(max_output_chars=10), output="0123456789abcdef")
    )
    result = asyncio.run(gateway.call("kb_search", {"query": "x"}, _auth()))
    assert result.ok
    assert str(result.output).startswith("0123456789")
    assert str(result.output).endswith("…[truncated]")
    assert audit.events[-1].detail["truncated"] is True


def test_timeout_returns_error_and_audits() -> None:
    gateway, audit = _gateway(FakeTool(_spec(timeout_s=0), delay=0.2))
    result = asyncio.run(gateway.call("kb_search", {"query": "x"}, _auth()))
    assert not result.ok
    assert result.error == "TOOL_TIMEOUT"
    assert audit.events[-1].detail["outcome"] == "TOOL_TIMEOUT"


def test_tool_exception_returns_failed() -> None:
    gateway, audit = _gateway(FakeTool(_spec(), exc=RuntimeError("boom")))
    result = asyncio.run(gateway.call("kb_search", {"query": "x"}, _auth()))
    assert not result.ok
    assert result.error == "TOOL_FAILED"
    assert audit.events[-1].detail["error"] == "boom"


def test_successful_result_converts_to_tool_evidence() -> None:
    result = ToolResult(tool="kb_search", ok=True, output="住宿标准为 800 元", latency_ms=3)
    evidence = to_evidence(result)
    assert evidence is not None
    assert evidence.source_type == SourceType.TOOL
    assert evidence.source_name == "kb_search"
    assert evidence.id.startswith("tool:kb_search:")
    assert evidence.content == "住宿标准为 800 元"


def test_failed_result_does_not_convert_to_evidence() -> None:
    assert to_evidence(ToolResult(tool="kb_search", ok=False, error="TOOL_FAILED")) is None
