"""MCP stdio JSON-RPC 2.0 最小客户端（仅标准库 asyncio）。

只实现客服场景需要的子集：``initialize`` → ``notifications/initialized`` →
``tools/list`` → ``tools/call``。Server 以子进程方式启动，消息为换行分隔 JSON。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import Callable
from typing import Any

from zcaisagent.ports.security import AuthContext
from zcaisagent.ports.tools import ToolResult, ToolSpec

PROTOCOL_VERSION = "2024-11-05"
_CLOSE_WAIT_S = 1.0


class McpError(RuntimeError):
    """MCP 协议错误。"""


class McpUnavailable(McpError):
    """MCP Server 无法启动或连接，装配层据此跳过注册。"""


class McpToolError(McpError):
    """MCP 工具返回 isError 或结果结构非法。"""


def _content_text(result: dict) -> str:
    parts = [
        str(item.get("text", ""))
        for item in result.get("content") or []
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    return "\n".join(parts)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


class McpStdioClient:
    """一次会话内的 stdio 客户端；跨事件循环复用请重建实例。"""

    def __init__(
        self,
        command: list[str],
        env: dict[str, str] | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self._command = [str(part) for part in command]
        self._env = dict(env or {})
        self._timeout_s = timeout_s
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict]] = {}
        self._next_id = 0

    @property
    def started(self) -> bool:
        return self._process is not None

    async def start(self) -> None:
        if self._process is not None:
            return
        env = None
        if self._env:
            env = {**os.environ, **self._env}
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
        except (OSError, ValueError) as exc:
            raise McpUnavailable(
                f"failed to start mcp server {self._command!r}: {exc}"
            ) from exc
        self._reader = asyncio.create_task(self._read_loop())

    async def initialize(self) -> None:
        await self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "zcaisagent", "version": "0.1.0"},
            },
        )
        await self.notify("notifications/initialized", {})

    async def list_tools(self) -> list[dict]:
        result = await self.request("tools/list", {})
        tools = result.get("tools") if isinstance(result, dict) else None
        return [item for item in tools or [] if isinstance(item, dict)]

    async def call_tool(self, name: str, args: dict) -> str:
        result = await self.request("tools/call", {"name": name, "arguments": args})
        if not isinstance(result, dict):
            raise McpToolError(f"invalid tools/call result for {name}")
        if result.get("isError"):
            raise McpToolError(_content_text(result) or f"tool error: {name}")
        return _content_text(result)

    async def request(self, method: str, params: dict) -> dict:
        if self._process is None:
            raise McpUnavailable("mcp client is not started")
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            return await asyncio.wait_for(future, timeout=self._timeout_s)
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def close(self) -> None:
        process, reader = self._process, self._reader
        self._process = None
        self._reader = None
        self._fail_pending(McpUnavailable("mcp client closed"))
        if reader is not None and not reader.done():
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        if process is None:
            return
        if process.stdin is not None:
            with contextlib.suppress(OSError):
                process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=_CLOSE_WAIT_S)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()

    async def _send(self, message: dict) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise McpUnavailable("mcp client is not started")
        payload = json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
        try:
            process.stdin.write(payload)
            await process.stdin.drain()
        except (OSError, RuntimeError) as exc:
            raise McpUnavailable(f"mcp server closed the pipe: {exc}") from exc

    async def _read_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            request_id = message.get("id")
            if request_id is None:
                continue
            future = self._pending.get(request_id)
            if future is None or future.done():
                continue
            error = message.get("error")
            if error:
                text = error.get("message") if isinstance(error, dict) else str(error)
                future.set_exception(McpError(str(text or "mcp error")))
            else:
                future.set_result(message.get("result") or {})
        self._fail_pending(McpError("mcp server closed the connection"))

    def _fail_pending(self, exc: Exception) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()


def _input_schema(tool: dict) -> dict:
    raw = tool.get("inputSchema") or tool.get("input_schema") or {}
    properties: dict[str, Any] = {}
    required: list[str] = []
    if isinstance(raw, dict):
        properties = {str(key): value for key, value in (raw.get("properties") or {}).items()}
        required = [str(item) for item in raw.get("required") or []]
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


class McpToolAdapter:
    """把一个 MCP tool 映射为只读 ToolPort。

    ``client_factory`` 在每次调用时建立新会话，避免子进程绑定到已关闭的事件循环。
    """

    def __init__(
        self,
        client_factory: Callable[[], McpStdioClient],
        tool: dict,
        *,
        name_prefix: str = "",
        timeout_s: float = 20.0,
    ) -> None:
        mcp_name = str(tool.get("name") or "")
        if not mcp_name:
            raise McpUnavailable("mcp tool without name")
        self._mcp_name = mcp_name
        self._client_factory = client_factory
        self.spec = ToolSpec(
            name=f"{name_prefix}{mcp_name}",
            description=str(tool.get("description") or f"MCP 工具 {mcp_name}"),
            input_schema=_input_schema(tool),
            risk_level="read",
            scopes=["tool:read"],
            timeout_s=int(timeout_s),
        )

    async def invoke(self, args: dict, ctx: AuthContext) -> ToolResult:
        started = time.monotonic()
        client = self._client_factory()
        try:
            await client.start()
            await client.initialize()
            output = await client.call_tool(self._mcp_name, args)
        except TimeoutError:
            return ToolResult(
                tool=self.spec.name, ok=False, error="TOOL_TIMEOUT",
                latency_ms=_elapsed_ms(started),
            )
        except (McpError, OSError):
            return ToolResult(
                tool=self.spec.name, ok=False, error="TOOL_FAILED",
                latency_ms=_elapsed_ms(started),
            )
        finally:
            await client.close()
        return ToolResult(
            tool=self.spec.name, ok=True, output=output, latency_ms=_elapsed_ms(started)
        )


def server_command(server: dict) -> list[str]:
    raw = server.get("command")
    if isinstance(raw, list):
        command = [str(part) for part in raw]
    elif isinstance(raw, str) and raw:
        command = [raw, *[str(arg) for arg in server.get("args") or []]]
    else:
        raise McpUnavailable("mcp server command is missing")
    return command


def client_factory(server: dict) -> Callable[[], McpStdioClient]:
    command = server_command(server)
    env = {str(key): str(value) for key, value in (server.get("env") or {}).items()}
    timeout_s = float(server.get("timeout_s", 20))

    def factory() -> McpStdioClient:
        return McpStdioClient(command, env=env or None, timeout_s=timeout_s)

    return factory


async def discover_mcp_tools(server: dict) -> list[McpToolAdapter]:
    """启动一次会话发现工具；失败统一抛 McpUnavailable 供装配层降级。"""
    factory = client_factory(server)
    timeout_s = float(server.get("timeout_s", 20))
    prefix = str(server.get("prefix") or "")
    client = factory()
    try:
        await client.start()
        await client.initialize()
        tools = await client.list_tools()
    except TimeoutError as exc:
        raise McpUnavailable(f"mcp server timeout: {exc}") from exc
    except McpError as exc:
        raise McpUnavailable(str(exc)) from exc
    finally:
        await client.close()
    return [
        McpToolAdapter(factory, tool, name_prefix=prefix, timeout_s=timeout_s)
        for tool in tools
    ]
