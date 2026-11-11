"""OpenAPI 3.x schema → ToolSpec 映射（纯解析，不做 HTTP 调用）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_JSON_MEDIA = "application/json"
_METHODS = ("get", "post", "put", "patch", "delete")


@dataclass(frozen=True)
class OpenApiOperation:
    """一个白名单内的 operation，可直接映射为 ToolPort。"""

    name: str
    operation_id: str
    method: str
    path: str
    base_url: str
    input_schema: dict
    parameters: list[dict] = field(default_factory=list)
    request_body: dict | None = None
    security: list[dict] = field(default_factory=list)
    description: str = ""


def _resolve_schema(spec: dict, schema: Any) -> dict:
    """解析本地 ``#/components/...`` 引用；解析失败返回空 schema。"""
    if not isinstance(schema, dict):
        return {}
    ref = schema.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return schema
    node: Any = spec
    for part in ref[2:].split("/"):
        node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            return {}
    return node if isinstance(node, dict) else {}


def _security(spec: dict, operation: dict) -> list[dict]:
    raw = operation.get("security")
    if raw is None:
        raw = spec.get("security")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _parameter_properties(spec: dict, parameters: list) -> tuple[dict, list[str]]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for raw in parameters:
        parameter = _resolve_schema(spec, raw)
        name = str(parameter.get("name") or "")
        if not name:
            continue
        schema = dict(_resolve_schema(spec, parameter.get("schema")))
        if parameter.get("in") == "path":
            schema.setdefault("type", "string")
        if parameter.get("description"):
            schema.setdefault("description", str(parameter["description"]))
        properties[name] = schema
        if parameter.get("required") or parameter.get("in") == "path":
            required.append(name)
    return properties, required


def _body_schema(spec: dict, request_body: Any) -> dict:
    if not isinstance(request_body, dict):
        return {}
    content = request_body.get("content") or {}
    media = content.get(_JSON_MEDIA) if isinstance(content, dict) else None
    if not isinstance(media, dict):
        return {}
    return _resolve_schema(spec, media.get("schema"))


def _input_schema(spec: dict, parameters: list, request_body: Any) -> dict:
    properties, required = _parameter_properties(spec, parameters)
    body = _body_schema(spec, request_body)
    for name, schema in (body.get("properties") or {}).items():
        properties.setdefault(str(name), schema if isinstance(schema, dict) else {})
    required.extend(str(item) for item in body.get("required") or [])
    unique = list(dict.fromkeys(required))
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if unique:
        schema["required"] = unique
    return schema


def operation_specs(
    spec: dict,
    allowed_operations: list[str],
    base_url: str,
    prefix: str = "",
) -> list[OpenApiOperation]:
    """按 operationId 白名单生成 operation；白名单外的 operation 不生成工具。"""
    allowed = [str(item) for item in allowed_operations]
    if not allowed:
        return []
    base = str(base_url).rstrip("/")
    found: dict[str, OpenApiOperation] = {}
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method in _METHODS:
            operation = item.get(method)
            if not isinstance(operation, dict):
                continue
            operation_id = str(operation.get("operationId") or "")
            if operation_id not in allowed or operation_id in found:
                continue
            parameters = [
                *(item.get("parameters") or []),
                *(operation.get("parameters") or []),
            ]
            request_body = operation.get("requestBody")
            found[operation_id] = OpenApiOperation(
                name=f"{prefix}{operation_id}",
                operation_id=operation_id,
                method=method.upper(),
                path=str(path),
                base_url=base,
                input_schema=_input_schema(spec, parameters, request_body),
                parameters=[item for item in parameters if isinstance(item, dict)],
                request_body=request_body if isinstance(request_body, dict) else None,
                security=_security(spec, operation),
                description=str(
                    operation.get("summary")
                    or operation.get("description")
                    or operation_id
                ),
            )
    return [found[operation_id] for operation_id in allowed if operation_id in found]
