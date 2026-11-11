"""OpenAPI 工具适配器：按白名单 operation 发起 HTTP 调用，密钥经 SecretPort 获取。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from zcaisagent.capabilities.tools.openapi_adapter import (
    OpenApiOperation,
    operation_specs,
)
from zcaisagent.ports.security import AuthContext, SecretPort
from zcaisagent.ports.tools import ToolResult, ToolSpec

_YAML_SUFFIXES = (".yaml", ".yml")


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


class OpenApiTool:
    """一个白名单 operation 对应一个只读工具。"""

    def __init__(
        self,
        operation: OpenApiOperation,
        *,
        secret: SecretPort | None = None,
        secret_key: str = "",
        header_name: str = "Authorization",
        header_prefix: str = "Bearer ",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 20.0,
    ) -> None:
        self.operation = operation
        self.spec = ToolSpec(
            name=operation.name,
            description=operation.description or f"{operation.method} {operation.path}",
            input_schema=operation.input_schema,
            risk_level="read",
            scopes=["tool:read"],
            timeout_s=int(timeout_s),
            idempotent=operation.method == "GET",
        )
        self._secret = secret
        self._secret_key = secret_key
        self._header_name = header_name
        self._header_prefix = header_prefix
        self._transport = transport
        self._timeout_s = timeout_s
        self._locations = {
            str(parameter.get("name")): str(parameter.get("in") or "query")
            for parameter in operation.parameters
            if parameter.get("name")
        }

    async def invoke(self, args: dict, ctx: AuthContext) -> ToolResult:
        started = time.monotonic()
        path = self.operation.path
        query: dict[str, object] = {}
        body: dict[str, object] = {}
        for key, value in args.items():
            location = self._locations.get(key)
            if location == "path":
                path = path.replace(f"{{{key}}}", quote(str(value), safe=""))
            elif location == "query":
                query[key] = value
            elif self.operation.request_body is not None:
                body[key] = value
            else:
                query[key] = value

        headers: dict[str, str] = {}
        if self._secret is not None and self._secret_key:
            key_value = await self._secret.get(self._secret_key)
            if key_value:
                headers[self._header_name] = f"{self._header_prefix}{key_value}"

        url = f"{self.operation.base_url}{path}"
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout_s
            ) as client:
                response = await client.request(
                    self.operation.method,
                    url,
                    params=query or None,
                    json=body or None,
                    headers=headers or None,
                )
        except httpx.HTTPError:
            return ToolResult(
                tool=self.spec.name,
                ok=False,
                error="TOOL_FAILED",
                latency_ms=_elapsed_ms(started),
            )

        if not 200 <= response.status_code < 300:
            return ToolResult(
                tool=self.spec.name,
                ok=False,
                error="TOOL_FAILED",
                latency_ms=_elapsed_ms(started),
            )
        return ToolResult(
            tool=self.spec.name,
            ok=True,
            output=response.text,
            latency_ms=_elapsed_ms(started),
        )


def load_openapi_spec(
    location: str | Path,
    *,
    client: httpx.Client | None = None,
    timeout_s: float = 10.0,
) -> dict:
    """加载 JSON 格式 OpenAPI spec；支持本地路径与 http(s) URL。"""
    target = str(location)
    if not target:
        raise ValueError("openapi spec location is empty")
    if target.lower().endswith(_YAML_SUFFIXES):
        raise ValueError("仅支持 JSON 格式的 OpenAPI spec，请先将 YAML 转换为 JSON")

    if target.startswith(("http://", "https://")):
        owns_client = client is None
        http = client or httpx.Client(timeout=timeout_s)
        try:
            response = http.get(target)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ValueError(f"无法加载 OpenAPI spec: {target}") from exc
        finally:
            if owns_client:
                http.close()
        if not isinstance(payload, dict):
            raise ValueError(f"OpenAPI spec 必须是 JSON 对象: {target}")
        return payload

    path = Path(target).expanduser()
    if not path.is_file():
        raise ValueError(f"OpenAPI spec 不存在: {target}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"OpenAPI spec 必须是 JSON 对象: {target}")
    return payload


def build_openapi_tools(
    source: dict,
    *,
    secret: SecretPort | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[OpenApiTool]:
    """按 source 配置生成白名单内的全部工具；无匹配 operation 时报错。"""
    location = str(source.get("spec") or source.get("spec_path") or "")
    spec = load_openapi_spec(location)
    operations = operation_specs(
        spec,
        [str(item) for item in source.get("operations") or []],
        str(source.get("base_url") or ""),
        prefix=str(source.get("prefix") or ""),
    )
    if not operations:
        raise ValueError("openapi source 没有匹配白名单的 operation")
    header_prefix = source.get("header_prefix")
    return [
        OpenApiTool(
            operation,
            secret=secret,
            secret_key=str(source.get("secret_key") or ""),
            header_name=str(source.get("header_name") or "Authorization"),
            header_prefix="Bearer " if header_prefix is None else str(header_prefix),
            transport=transport,
        )
        for operation in operations
    ]
