from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Callable
from typing import Any, Literal

from zcaisagent.capabilities.tools.builtin import KnowledgeBaseSearchTool
from zcaisagent.domain.enums import ApprovalStatus, SourceType
from zcaisagent.domain.models import Evidence, PendingApproval, ToolInvocation
from zcaisagent.governance.guards import detect_injection
from zcaisagent.governance.redaction import redact_pii
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

_TRUNCATED_SUFFIX = "…[truncated]"
_CIRCUIT_FAILURE_THRESHOLD = 5
_CIRCUIT_COOLDOWN_S = 60.0
_INVOCATION_STATUS: dict[str, Literal["approval_required", "rejected"]] = {
    "TOOL_REQUIRES_APPROVAL": "approval_required",
    "TOOL_REJECTED": "rejected",
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


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def args_hash(args: dict) -> str:
    """参数摘要取 sha256 前 12 位；审批与审计共用同一口径。"""
    canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    return _digest(canonical)


def _new_approval_id() -> str:
    return "ap_" + uuid.uuid4().hex[:12]


class ToolGateway:
    def __init__(
        self,
        audit: AuditPort | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._tools: dict[str, ToolPort] = {}
        self._audit = audit
        self._clock = clock
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}

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
        self,
        name: str,
        args: dict,
        auth: AuthContext,
        run_id: str = "",
        approval: PendingApproval | None = None,
    ) -> ToolResult:
        """四段校验：存在性 → scope → JSON Schema → 风险审批，然后执行。"""
        tool = self._tools.get(name)
        if tool is None:
            await self._record(name, "TOOL_NOT_FOUND", auth, run_id)
            return ToolResult(tool=name, ok=False, error="TOOL_NOT_FOUND")

        spec = tool.spec
        detail: dict[str, Any] = {
            "args_hash": args_hash(args),
            "risk_level": spec.risk_level,
        }
        missing = [scope for scope in spec.scopes if scope not in set(auth.scopes)]
        if missing:
            await self._record(
                name, "TOOL_UNAUTHORIZED", auth, run_id, {**detail, "missing": missing}
            )
            return ToolResult(tool=name, ok=False, error="TOOL_UNAUTHORIZED")

        errors = validate_args(spec.input_schema, args)
        if errors:
            await self._record(
                name, "TOOL_INVALID_ARGS", auth, run_id, {**detail, "errors": errors}
            )
            return ToolResult(tool=name, ok=False, error="TOOL_INVALID_ARGS")

        if spec.risk_level != "read" or spec.requires_approval:
            return await self._guard_approval(tool, args, auth, run_id, detail, approval)

        return await self._invoke(tool, args, auth, run_id, detail)

    async def _guard_approval(
        self,
        tool: ToolPort,
        args: dict,
        auth: AuthContext,
        run_id: str,
        detail: dict[str, Any],
        approval: PendingApproval | None,
    ) -> ToolResult:
        name = tool.spec.name
        if approval is not None and approval.status == ApprovalStatus.REJECTED:
            await self._record(
                name,
                "TOOL_REJECTED",
                auth,
                run_id,
                {**detail, "approval_id": approval.id},
                action="tool.rejected",
            )
            return ToolResult(
                tool=name, ok=False, error="TOOL_REJECTED", approval_id=approval.id
            )

        if approval is not None and approval.status == ApprovalStatus.APPROVED:
            if approval.tool != name or args_hash(approval.args) != detail["args_hash"]:
                await self._record(
                    name,
                    "TOOL_APPROVAL_MISMATCH",
                    auth,
                    run_id,
                    {**detail, "approval_id": approval.id},
                )
                return ToolResult(
                    tool=name,
                    ok=False,
                    error="TOOL_APPROVAL_MISMATCH",
                    approval_id=approval.id,
                )
            approved_detail = {**detail, "approval_id": approval.id}
            if approval.decided_by:
                approved_detail["approver"] = approval.decided_by
            return await self._invoke(tool, args, auth, run_id, approved_detail)

        approval_id = approval.id if approval is not None and approval.id else _new_approval_id()
        await self._record(
            name,
            "TOOL_REQUIRES_APPROVAL",
            auth,
            run_id,
            {**detail, "approval_id": approval_id},
            action="tool.approval_requested",
        )
        return ToolResult(
            tool=name,
            ok=False,
            error="TOOL_REQUIRES_APPROVAL",
            approval_required=True,
            approval_id=approval_id,
        )

    async def _invoke(
        self,
        tool: ToolPort,
        args: dict,
        auth: AuthContext,
        run_id: str,
        detail: dict[str, Any],
    ) -> ToolResult:
        name = tool.spec.name
        spec = tool.spec
        if self._circuit_open(name):
            await self._record(name, "TOOL_CIRCUIT_OPEN", auth, run_id, detail)
            return ToolResult(tool=name, ok=False, error="TOOL_CIRCUIT_OPEN")

        try:
            result = await asyncio.wait_for(
                tool.invoke(args, auth), timeout=spec.timeout_s
            )
        except TimeoutError:
            self._note_failure(name)
            await self._record(name, "TOOL_TIMEOUT", auth, run_id, detail)
            return ToolResult(tool=name, ok=False, error="TOOL_TIMEOUT")
        except Exception as exc:
            self._note_failure(name)
            await self._record(
                name, "TOOL_FAILED", auth, run_id, {**detail, "error": str(exc)}
            )
            return ToolResult(tool=name, ok=False, error="TOOL_FAILED")

        text = _as_text(result.output) if result.output is not None else ""
        audited = {**detail, "latency_ms": result.latency_ms}
        if text:
            audited["result_digest"] = _digest(text)

        if result.ok and detect_injection(text) is not None:
            await self._record(name, "TOOL_INJECTION_SUSPECTED", auth, run_id, audited)
            return ToolResult(
                tool=name,
                ok=False,
                error="TOOL_INJECTION_SUSPECTED",
                latency_ms=result.latency_ms,
                quarantined=True,
            )

        truncated = False
        if result.ok and text:
            sanitized = redact_pii(text)
            if len(sanitized) > spec.max_output_chars:
                sanitized = sanitized[: spec.max_output_chars] + _TRUNCATED_SUFFIX
                truncated = True
            if truncated or sanitized != text:
                result.output = sanitized

        if result.ok:
            self._reset_failures(name)
        else:
            self._note_failure(name)
        await self._record(
            name,
            "TOOL_OK" if result.ok else "TOOL_FAILED",
            auth,
            run_id,
            {**audited, "ok": result.ok, "truncated": truncated},
        )
        return result

    def _circuit_open(self, name: str) -> bool:
        opened_at = self._opened_at.get(name)
        if opened_at is None:
            return False
        if self._clock() - opened_at < _CIRCUIT_COOLDOWN_S:
            return True
        self._opened_at.pop(name, None)
        self._failures.pop(name, None)
        return False

    def _note_failure(self, name: str) -> None:
        count = self._failures.get(name, 0) + 1
        self._failures[name] = count
        if count >= _CIRCUIT_FAILURE_THRESHOLD:
            self._opened_at[name] = self._clock()

    def _reset_failures(self, name: str) -> None:
        self._failures.pop(name, None)
        self._opened_at.pop(name, None)

    async def _record(
        self,
        tool: str,
        outcome: str,
        auth: AuthContext,
        run_id: str,
        detail: dict | None = None,
        action: str = "tool.call",
    ) -> None:
        if self._audit is None:
            return
        payload = {"tool": tool, "outcome": outcome, **(detail or {})}
        await self._audit.append(
            AuditEvent(
                action=action,
                actor=auth.user_id,
                tenant_id=auth.tenant_id,
                run_id=run_id,
                detail=payload,
            )
        )


def to_invocation(
    result: ToolResult,
    args: dict,
    risk_level: Literal["read", "write", "destructive"] = "read",
) -> ToolInvocation:
    """把一次调用结果映射为审计用的 ToolInvocation。"""
    text = _as_text(result.output) if result.output is not None else ""
    status: Literal["ok", "error", "approval_required", "rejected"] = "ok"
    if not result.ok:
        status = _INVOCATION_STATUS.get(str(result.error), "error")
    return ToolInvocation(
        id="inv_" + uuid.uuid4().hex[:12],
        tool=result.tool,
        args_hash=args_hash(args),
        risk_level=risk_level,
        status=status,
        latency_ms=result.latency_ms,
        result_digest=_digest(text) if text else None,
        approval_id=result.approval_id,
        error=result.error,
    )


def to_evidence(result: ToolResult) -> Evidence | None:
    if not result.ok or result.quarantined or result.approval_required:
        return None
    if result.output is None:
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
