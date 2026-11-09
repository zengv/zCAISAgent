from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from zcaisagent.capabilities.tools.builtin import KnowledgeBaseSearchTool
from zcaisagent.domain.enums import SourceType
from zcaisagent.domain.models import Evidence
from zcaisagent.ports.retrieval import RetrieverPort
from zcaisagent.ports.security import AuditEvent, AuditPort, AuthContext
from zcaisagent.ports.tools import ToolPort, ToolResult, ToolSpec

_TYPE_MAP: dict[str, Any] = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "object": dict,
    "array": list,
}


def validate_args(schema: dict, args: dict) -> list[str]:
    """JSON schema 的最小子集校验：required / type / additionalProperties。"""
    errors: list[str] = []
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    if args and not properties:
        errors.append("unexpected:" + ",".join(sorted(args)))
    for name in required:
        if name not in args:
            errors.append(f"missing:{name}")
    for name, value in args.items():
        rule = properties.get(name)
        if name not in properties:
            if schema.get("additionalProperties") is False:
                errors.append(f"unexpected:{name}")
            continue
        if not isinstance(rule, dict):
            continue
        expected = rule.get("type")
        python_type = _TYPE_MAP.get(str(expected))
        if python_type is None:
            continue
        if isinstance(value, bool) and python_type in {(int, float), int}:
            errors.append(f"type:{name}")
        elif not isinstance(value, python_type):
            errors.append(f"type:{name}")
    return errors


class ToolGateway:
    def __init__(self, audit: AuditPort | None = None) -> None:
        self._tools: dict[str, ToolPort] = {}
        self._audit = audit

    def register(self, tool: ToolPort) -> None:
        if tool.spec.name in self._tools:
            raise ValueError(f"tool already registered: {tool.spec.name}")
        if not isinstance(tool.spec, ToolSpec):
            raise ValueError("tool.spec must be ToolSpec")
        self._tools[tool.spec.name] = tool

    def get(self, name: str) -> ToolPort | None:
        return self._tools.get(name)

    def all(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]

    async def call(
        self, name: str, args: dict, auth: AuthContext, run_id: str = ""
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            await self._record(name, "TOOL_NOT_FOUND", auth, run_id)
            return ToolResult(tool=name, ok=False, error="TOOL_NOT_FOUND")

        spec = tool.spec
        if spec.risk_level != "read" or spec.requires_approval:
            await self._record(name, "TOOL_REQUIRES_APPROVAL", auth, run_id)
            return ToolResult(tool=name, ok=False, error="TOOL_REQUIRES_APPROVAL")

        missing = [scope for scope in spec.scopes if scope not in set(auth.scopes)]
        if missing:
            await self._record(
                name, "TOOL_UNAUTHORIZED", auth, run_id, {"missing": missing}
            )
            return ToolResult(tool=name, ok=False, error="TOOL_UNAUTHORIZED")

        errors = validate_args(spec.input_schema, args)
        if errors:
            await self._record(name, "TOOL_INVALID_ARGS", auth, run_id, {"errors": errors})
            return ToolResult(tool=name, ok=False, error="TOOL_INVALID_ARGS")

        try:
            result = await asyncio.wait_for(
                tool.invoke(args, auth), timeout=spec.timeout_s
            )
        except TimeoutError:
            await self._record(name, "TOOL_TIMEOUT", auth, run_id)
            return ToolResult(tool=name, ok=False, error="TOOL_TIMEOUT")
        except Exception as exc:
            await self._record(name, "TOOL_FAILED", auth, run_id, {"error": str(exc)})
            return ToolResult(tool=name, ok=False, error="TOOL_FAILED")

        truncated = False
        if result.output is not None:
            text = (
                result.output
                if isinstance(result.output, str)
                else json.dumps(result.output, ensure_ascii=False, default=str)
            )
            if len(text) > spec.max_output_chars:
                result.output = text[: spec.max_output_chars] + "…[truncated]"
                truncated = True
        await self._record(
            name, "TOOL_OK" if result.ok else "TOOL_FAILED", auth, run_id,
            {"ok": result.ok, "truncated": truncated, "latency_ms": result.latency_ms},
        )
        return result

    async def _record(
        self,
        tool: str,
        outcome: str,
        auth: AuthContext,
        run_id: str,
        detail: dict | None = None,
    ) -> None:
        if self._audit is None:
            return
        payload = {"tool": tool, "outcome": outcome, **(detail or {})}
        await self._audit.append(
            AuditEvent(
                action="tool.call",
                actor=auth.user_id,
                tenant_id=auth.tenant_id,
                run_id=run_id,
                detail=payload,
            )
        )


def to_evidence(result: ToolResult) -> Evidence | None:
    if not result.ok or result.output is None:
        return None
    content = (
        result.output
        if isinstance(result.output, str)
        else json.dumps(result.output, ensure_ascii=False, default=str)
    )
    if not content.strip():
        return None
    digest = hashlib.sha1(content.encode("utf-8")).hexdigest()[:10]
    return Evidence(
        id=f"tool:{result.tool}:{digest}",
        source_type=SourceType.TOOL,
        source_name=result.tool,
        title=f"tool:{result.tool}",
        content=content,
        snippet=content[:160],
        score=0.5,
        relevance=0.5,
        authority="primary",
        metadata={"latency_ms": result.latency_ms},
    )


def build_default_tool_gateway(
    audit: AuditPort | None = None, retriever: RetrieverPort | None = None
) -> ToolGateway:
    """建立只读网关；传入 retriever 时注册内置 kb_search 工具。"""
    gateway = ToolGateway(audit=audit)
    if retriever is not None:
        gateway.register(KnowledgeBaseSearchTool(retriever))
    return gateway
