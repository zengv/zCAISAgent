import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest

from tests.conftest import make_chunk
from zcaisagent.adapters.health.memory import HealthBoard
from zcaisagent.adapters.tools.openapi import (
    OpenApiTool,
    build_openapi_tools,
    load_openapi_spec,
)
from zcaisagent.capabilities.retrieval.sources.inmemory import InMemoryRetriever
from zcaisagent.capabilities.tools.builtin import (
    TicketCreateTool,
    TicketListTool,
    TicketStore,
)
from zcaisagent.capabilities.tools.gateway import (
    ToolGateway,
    build_default_tool_gateway,
    validate_args,
)
from zcaisagent.capabilities.tools.mcp_client import (
    McpStdioClient,
    McpToolAdapter,
    McpUnavailable,
    discover_mcp_tools,
)
from zcaisagent.capabilities.tools.openapi_adapter import (
    OpenApiOperation,
    operation_specs,
)
from zcaisagent.capabilities.tools.sql_tool import SqlReadonlyTool
from zcaisagent.container import build_configured_tools
from zcaisagent.domain.enums import ApprovalStatus
from zcaisagent.domain.models import PendingApproval
from zcaisagent.ports.health import HealthStatus
from zcaisagent.ports.security import AuthContext
from zcaisagent.ports.tools import ToolResult, ToolSpec
from zcaisagent.settings import Settings, ToolsConfig

_SERVER = Path(__file__).resolve().parents[1] / "fakes" / "mcp_echo_server.py"
_MISSING_COMMAND = "zcaisagent-no-such-command-xyz"


class FakeSecret:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.calls: list[str] = []

    async def get(self, key: str) -> str:
        self.calls.append(key)
        return self.values.get(key, "")


def _auth(scopes: list[str] | None = None) -> AuthContext:
    return AuthContext(
        tenant_id="demo",
        user_id="tester",
        scopes=["tool:read"] if scopes is None else scopes,
    )


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


def _server_config(**overrides: object) -> dict:
    config: dict = {
        "name": "echo",
        "command": sys.executable,
        "args": [str(_SERVER)],
        "timeout_s": 5,
    }
    config.update(overrides)
    return config


def test_mcp_stdio_initialize_list_and_call() -> None:
    async def scenario() -> None:
        client = McpStdioClient([sys.executable, str(_SERVER)], timeout_s=5)
        await client.start()
        try:
            await client.initialize()
            tools = await client.list_tools()
            assert {item["name"] for item in tools} == {"echo", "add"}
            assert await client.call_tool("echo", {"text": "hi"}) == "hi\ndone"
            assert float(await client.call_tool("add", {"a": 1, "b": 2})) == 3.0
        finally:
            await client.close()

    asyncio.run(scenario())


def test_mcp_call_timeout_raises() -> None:
    async def scenario() -> None:
        client = McpStdioClient([sys.executable, str(_SERVER)], timeout_s=1.5)
        await client.start()
        try:
            await client.initialize()
            with pytest.raises(TimeoutError):
                await client.call_tool("echo", {"text": "slow", "delay": 2.5})
        finally:
            await client.close()

    asyncio.run(scenario())


def test_mcp_unavailable_command_raises() -> None:
    async def scenario() -> None:
        client = McpStdioClient([_MISSING_COMMAND], timeout_s=1)
        with pytest.raises(McpUnavailable):
            await client.start()

    asyncio.run(scenario())

    with pytest.raises(McpUnavailable):
        asyncio.run(discover_mcp_tools({"name": "bad", "command": _MISSING_COMMAND}))


def test_mcp_adapter_maps_spec_and_invokes() -> None:
    adapters = asyncio.run(discover_mcp_tools(_server_config(prefix="mcp_")))
    echo = next(item for item in adapters if item.spec.name == "mcp_echo")

    assert echo.spec.risk_level == "read"
    assert echo.spec.scopes == ["tool:read"]
    assert echo.spec.input_schema["type"] == "object"
    assert echo.spec.input_schema["required"] == ["text"]
    assert echo.spec.input_schema["additionalProperties"] is False
    assert validate_args(echo.spec.input_schema, {"text": "x"}) == []
    assert validate_args(echo.spec.input_schema, {}) == ["missing:text"]

    result = asyncio.run(echo.invoke({"text": "hello"}, _auth()))
    assert isinstance(result, ToolResult)
    assert result.ok is True
    assert result.output == "hello\ndone"

    failed = asyncio.run(echo.invoke({"text": "__error__"}, _auth()))
    assert failed.ok is False
    assert failed.error == "TOOL_FAILED"


class _TimeoutClient:
    async def start(self) -> None:
        return None

    async def initialize(self) -> None:
        return None

    async def call_tool(self, name: str, args: dict) -> str:
        raise TimeoutError

    async def close(self) -> None:
        return None


def test_mcp_adapter_maps_timeout_to_tool_timeout() -> None:
    adapter = McpToolAdapter(
        lambda: _TimeoutClient(),
        {"name": "echo", "inputSchema": {"type": "object", "properties": {}}},
    )
    result = asyncio.run(adapter.invoke({}, _auth()))
    assert result.ok is False
    assert result.error == "TOOL_TIMEOUT"


# ---------------------------------------------------------------------------
# OpenAPI
# ---------------------------------------------------------------------------

_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Support API", "version": "1.0"},
    "security": [{"apiKey": []}],
    "paths": {
        "/tickets/{ticket_id}": {
            "parameters": [
                {
                    "name": "ticket_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                }
            ],
            "get": {
                "operationId": "http_api",
                "summary": "读取工单",
                "parameters": [{"name": "expand", "in": "query", "schema": {"type": "string"}}],
            },
            "delete": {"operationId": "deleteTicket"},
        },
        "/tickets": {
            "post": {
                "operationId": "createTicket",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["subject"],
                                "properties": {
                                    "subject": {"type": "string"},
                                    "description": {"type": "string"},
                                },
                            }
                        }
                    },
                },
            }
        },
    },
}


def _get_operation() -> OpenApiOperation:
    return operation_specs(_SPEC, ["http_api"], "https://api.example.com")[0]


def test_operation_specs_filters_whitelist_and_merges_schema() -> None:
    operations = operation_specs(
        _SPEC, ["http_api", "createTicket"], "https://api.example.com"
    )
    assert [item.name for item in operations] == ["http_api", "createTicket"]

    get_spec = operations[0]
    assert get_spec.method == "GET"
    assert get_spec.path == "/tickets/{ticket_id}"
    assert get_spec.security == [{"apiKey": []}]
    assert set(get_spec.input_schema["properties"]) == {"ticket_id", "expand"}
    assert get_spec.input_schema["required"] == ["ticket_id"]
    assert get_spec.input_schema["additionalProperties"] is False

    post_spec = operations[1]
    assert set(post_spec.input_schema["properties"]) == {"subject", "description"}
    assert post_spec.input_schema["required"] == ["subject"]
    assert "deleteTicket" not in {item.name for item in operations}


def test_openapi_tool_builds_path_query_and_secret_header() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"id": 42, "expand": True})

    secret = FakeSecret({"openapi_key": "s3cr3t"})
    tool = OpenApiTool(
        _get_operation(),
        secret=secret,
        secret_key="openapi_key",
        header_name="X-API-Key",
        header_prefix="",
        transport=httpx.MockTransport(handler),
    )
    args = {"ticket_id": "42", "expand": "comments"}
    assert validate_args(tool.spec.input_schema, args) == []

    result = asyncio.run(tool.invoke(args, _auth()))

    assert result.ok is True
    assert json.loads(result.output) == {"id": 42, "expand": True}
    assert captured["url"] == "https://api.example.com/tickets/42?expand=comments"
    assert captured["headers"]["x-api-key"] == "s3cr3t"
    assert secret.calls == ["openapi_key"]


def test_openapi_tool_sends_json_body() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(201, json={"ticket_id": "T1"})

    operation = operation_specs(_SPEC, ["createTicket"], "https://api.example.com")[0]
    tool = OpenApiTool(operation, transport=httpx.MockTransport(handler))
    result = asyncio.run(
        tool.invoke({"subject": "VPN 故障", "description": "无法连接"}, _auth())
    )

    assert result.ok is True
    assert captured == {
        "method": "POST",
        "body": {"subject": "VPN 故障", "description": "无法连接"},
    }


def test_openapi_tool_maps_non_2xx_to_tool_failed() -> None:
    tool = OpenApiTool(
        _get_operation(),
        transport=httpx.MockTransport(lambda request: httpx.Response(500, text="boom")),
    )
    result = asyncio.run(tool.invoke({"ticket_id": "1"}, _auth()))

    assert result.ok is False
    assert result.error == "TOOL_FAILED"


def test_load_openapi_spec_from_file_and_rejects_yaml(tmp_path: Path) -> None:
    spec_path = tmp_path / "openapi.json"
    spec_path.write_text(json.dumps(_SPEC), encoding="utf-8")
    assert load_openapi_spec(spec_path)["openapi"] == "3.0.3"

    yaml_path = tmp_path / "openapi.yaml"
    yaml_path.write_text("openapi: 3.0.3", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON"):
        load_openapi_spec(yaml_path)


def test_load_openapi_spec_from_url() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=_SPEC))
    with httpx.Client(transport=transport) as client:
        spec = load_openapi_spec("https://api.example.com/openapi.json", client=client)
    assert spec["openapi"] == "3.0.3"


def test_build_openapi_tools_requires_whitelist_match(tmp_path: Path) -> None:
    spec_path = tmp_path / "openapi.json"
    spec_path.write_text(json.dumps(_SPEC), encoding="utf-8")
    source = {
        "spec": str(spec_path),
        "base_url": "https://api.example.com",
        "operations": ["http_api"],
    }
    tools = build_openapi_tools(source)
    assert [tool.spec.name for tool in tools] == ["http_api"]

    with pytest.raises(ValueError):
        build_openapi_tools(
            {"spec": str(spec_path), "base_url": "", "operations": ["unknownOperation"]}
        )


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------


def _sql_db(tmp_path: Path, rows: int = 3) -> Path:
    db_path = tmp_path / "support.db"
    connection = sqlite3.connect(db_path)
    connection.execute("create table tickets (id integer primary key, subject text)")
    connection.executemany(
        "insert into tickets (subject) values (?)",
        [(f"t{index}",) for index in range(rows)],
    )
    connection.execute("create table secret_notes (id integer, body text)")
    connection.commit()
    connection.close()
    return db_path


def _sql_tool(tmp_path: Path, rows: int = 3, max_rows: int = 100) -> SqlReadonlyTool:
    return SqlReadonlyTool(_sql_db(tmp_path, rows), ["tickets"], max_rows=max_rows)


def test_sql_tool_returns_rows_as_json(tmp_path: Path) -> None:
    tool = _sql_tool(tmp_path)
    result = asyncio.run(
        tool.invoke({"sql": "select id, subject from tickets order by id"}, _auth())
    )

    assert result.ok is True
    assert tool.spec.risk_level == "read"
    assert tool.spec.scopes == ["tool:read"]
    rows = json.loads(result.output)
    assert [row["subject"] for row in rows] == ["t0", "t1", "t2"]


def test_sql_tool_supports_params(tmp_path: Path) -> None:
    tool = _sql_tool(tmp_path)
    result = asyncio.run(
        tool.invoke(
            {"sql": "select subject from tickets where id = ?", "params": [2]}, _auth()
        )
    )
    assert result.ok is True
    assert json.loads(result.output) == [{"subject": "t1"}]


def test_sql_tool_caps_rows(tmp_path: Path) -> None:
    db_path = _sql_db(tmp_path, rows=20)
    tool = SqlReadonlyTool(db_path, ["tickets"], max_rows=5)
    result = asyncio.run(tool.invoke({"sql": "select * from tickets"}, _auth()))
    assert result.ok is True
    assert len(json.loads(result.output)) == 5

    limited = asyncio.run(
        tool.invoke({"sql": "select * from tickets limit 100"}, _auth())
    )
    assert len(json.loads(limited.output)) == 5


def test_sql_tool_allows_cte(tmp_path: Path) -> None:
    tool = _sql_tool(tmp_path)
    result = asyncio.run(
        tool.invoke(
            {"sql": "with recent as (select * from tickets) select * from recent"},
            _auth(),
        )
    )
    assert result.ok is True
    assert len(json.loads(result.output)) == 3


@pytest.mark.parametrize(
    "sql",
    [
        "select * from secret_notes",
        "select * from tickets; drop table tickets",
        "delete from tickets",
        "pragma table_info(tickets)",
        "select 1",
    ],
)
def test_sql_tool_rejects_unsafe_statements(tmp_path: Path, sql: str) -> None:
    tool = _sql_tool(tmp_path)
    result = asyncio.run(tool.invoke({"sql": sql}, _auth()))
    assert result.ok is False
    assert result.error == "TOOL_INVALID_ARGS"


def test_sql_tool_reports_missing_database(tmp_path: Path) -> None:
    tool = SqlReadonlyTool(tmp_path / "missing.db", ["tickets"])
    result = asyncio.run(tool.invoke({"sql": "select * from tickets"}, _auth()))
    assert result.ok is False
    assert result.error == "TOOL_FAILED"


# ---------------------------------------------------------------------------
# 工单（写工具审批 + 幂等）
# ---------------------------------------------------------------------------

_TICKET_ARGS = {
    "subject": "VPN 无法连接",
    "description": "远程办公无法访问内网系统",
    "priority": "high",
    "idempotency_key": "idem-1",
}


def _ticket_gateway() -> ToolGateway:
    store = TicketStore()
    gateway = ToolGateway()
    gateway.register(TicketCreateTool(store))
    gateway.register(TicketListTool(store))
    return gateway


def test_ticket_create_requires_approval_then_is_idempotent() -> None:
    gateway = _ticket_gateway()
    auth = _auth(["tool:read", "tool:write"])

    pending = asyncio.run(gateway.call("ticket_create", _TICKET_ARGS, auth))
    assert pending.ok is False
    assert pending.error == "TOOL_REQUIRES_APPROVAL"
    assert pending.approval_required is True
    assert pending.approval_id

    approval = PendingApproval(
        id=pending.approval_id or "",
        tool="ticket_create",
        args=_TICKET_ARGS,
        risk_level="write",
        status=ApprovalStatus.APPROVED,
        decided_by="alice",
    )
    created = asyncio.run(
        gateway.call("ticket_create", _TICKET_ARGS, auth, approval=approval)
    )
    assert created.ok is True
    payload = json.loads(created.output)
    assert payload["created"] is True
    ticket_id = payload["ticket_id"]

    again = asyncio.run(
        gateway.call("ticket_create", _TICKET_ARGS, auth, approval=approval)
    )
    repeat_payload = json.loads(again.output)
    assert repeat_payload["ticket_id"] == ticket_id
    assert repeat_payload["created"] is False

    listed = asyncio.run(gateway.call("ticket_list", {"limit": 10}, auth))
    assert listed.ok is True
    assert [item["ticket_id"] for item in json.loads(listed.output)] == [ticket_id]


def test_ticket_create_requires_write_scope() -> None:
    gateway = _ticket_gateway()
    result = asyncio.run(gateway.call("ticket_create", _TICKET_ARGS, _auth()))
    assert result.ok is False
    assert result.error == "TOOL_UNAUTHORIZED"


def test_ticket_list_is_read_only() -> None:
    gateway = _ticket_gateway()
    spec = gateway.get("ticket_list")
    assert spec is not None
    assert spec.spec.risk_level == "read"
    assert spec.spec.requires_approval is False
    assert spec.spec.scopes == ["tool:read"]

    result = asyncio.run(gateway.call("ticket_list", {"limit": 5}, _auth()))
    assert result.ok is True
    assert json.loads(result.output) == []


# ---------------------------------------------------------------------------
# 契约：三个只读工具共享 ToolSpec / ToolResult / 鉴权语义
# ---------------------------------------------------------------------------


def _read_only_gateway(tmp_path: Path) -> ToolGateway:
    retriever = InMemoryRetriever()
    retriever.add([make_chunk("kb#0", "一线城市住宿标准为 800 元/晚。")])
    gateway = build_default_tool_gateway(retriever=retriever)
    gateway.register(SqlReadonlyTool(_sql_db(tmp_path), ["tickets"]))
    gateway.register(
        OpenApiTool(
            _get_operation(),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"id": 1})
            ),
        )
    )
    return gateway


def test_read_only_tools_share_contract(tmp_path: Path) -> None:
    gateway = _read_only_gateway(tmp_path)
    calls = {
        "kb_search": {"query": "住宿标准"},
        "http_api": {"ticket_id": "1"},
        "sql_readonly": {"sql": "select id from tickets"},
    }

    specs = {spec.name: spec for spec in gateway.all()}
    assert set(calls) <= set(specs)
    for spec in specs.values():
        assert isinstance(spec, ToolSpec)
        assert spec.risk_level == "read"
        assert "tool:read" in spec.scopes
        assert spec.input_schema.get("type") == "object"
        assert spec.input_schema.get("additionalProperties") is False
        assert spec.timeout_s > 0

    for name, args in calls.items():
        result = asyncio.run(gateway.call(name, args, _auth()))
        assert isinstance(result, ToolResult)
        assert result.ok is True, result.error

    for name, args in calls.items():
        denied = asyncio.run(gateway.call(name, args, _auth([])))
        assert isinstance(denied, ToolResult)
        assert denied.ok is False
        assert denied.error == "TOOL_UNAUTHORIZED"


# ---------------------------------------------------------------------------
# 装配：按配置注册 + 失败降级
# ---------------------------------------------------------------------------


def test_build_configured_tools_registers_and_degrades(tmp_path: Path) -> None:
    db_path = _sql_db(tmp_path)
    settings = Settings(
        tools=ToolsConfig(
            mcp_servers=[
                {"name": "broken", "command": _MISSING_COMMAND, "enabled": True}
            ],
            sql={
                "sources": {
                    "support": {
                        "path": str(db_path),
                        "tables": ["tickets"],
                        "max_rows": 5,
                    }
                }
            },
            enable_kb_search=False,
            enable_ticket=True,
        )
    )
    health = HealthBoard()

    gateway = build_configured_tools(settings, health=health)

    names = {spec.name for spec in gateway.all()}
    assert {"ticket_create", "ticket_list", "sql_readonly"} <= names
    statuses = {entry.name: entry.status for entry in health.snapshot()}
    assert statuses["tools.mcp.broken"] == HealthStatus.DOWN
    assert statuses["tools.sql.support"] == HealthStatus.OK


def test_build_configured_tools_without_config_keeps_kb_only(tmp_path: Path) -> None:
    retriever = InMemoryRetriever()
    gateway = build_configured_tools(Settings(), retriever=retriever)
    assert [spec.name for spec in gateway.all()] == ["kb_search"]


def test_settings_parses_nested_tools_config(tmp_path: Path, monkeypatch) -> None:
    for key in list(os.environ):
        if key.startswith("ZCAISAGENT_"):
            monkeypatch.delenv(key)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[tools]
enable_kb_search = false

[tools.builtin]
ticket_enabled = true

[tools.mcp.servers.echo]
command = "python"
args = ["-m", "demo"]
enabled = false

[tools.openapi.sources.api]
spec = "openapi.json"
base_url = "https://api.example.com"
operations = ["http_api"]
enabled = false

[tools.sql.sources.support]
path = "support.db"
tables = ["tickets"]
enabled = false
""",
        encoding="utf-8",
    )
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")

    settings = Settings.from_env(env_file=env_path, config_file=config_path)

    assert settings.tools is not None
    assert settings.tools.enable_kb_search is False
    assert settings.tools.enable_ticket is True
    assert settings.tools.mcp_servers == [
        {"name": "echo", "command": "python", "args": ["-m", "demo"], "enabled": False}
    ]
    assert set(settings.tools.openapi["sources"]) == {"api"}
    assert set(settings.tools.sql["sources"]) == {"support"}
