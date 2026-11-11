from __future__ import annotations

import json
import threading
import time
from typing import Any

from zcaisagent.ports.retrieval import RetrieveRequest, RetrieverPort
from zcaisagent.ports.security import AuthContext
from zcaisagent.ports.tools import ToolResult, ToolSpec

_PRIORITIES = {"low", "normal", "high"}


class KnowledgeBaseSearchTool:
    """内置只读工具：按 query 检索知识库片段，输出可直接转为证据的文本。"""

    spec = ToolSpec(
        name="kb_search",
        description="检索企业知识库；只读，返回命中的文档片段。",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        risk_level="read",
        scopes=["tool:read"],
        timeout_s=20,
        idempotent=True,
        max_output_chars=8_000,
    )

    def __init__(self, retriever: RetrieverPort) -> None:
        self._retriever = retriever

    async def invoke(self, args: dict, ctx: AuthContext) -> ToolResult:
        request = RetrieveRequest(
            query=str(args["query"]),
            top_k=int(args.get("top_k") or 5),
            tenant_id=ctx.tenant_id,
            acl_tokens=ctx.acl_tokens,
        )
        result = await self._retriever.retrieve(request, ctx)
        output = "\n".join(
            f"{item.title or item.source_name}：{item.content}"
            for item in result.evidence
        )
        return ToolResult(
            tool=self.spec.name,
            ok=True,
            output=output,
            latency_ms=result.latency_ms,
        )


class TicketStore:
    """进程内工单存储；按租户 + 幂等键去重。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tickets: dict[str, dict[str, Any]] = {}
        self._by_key: dict[tuple[str, str], str] = {}
        self._seq = 0

    def create(
        self,
        *,
        tenant_id: str,
        subject: str,
        description: str,
        priority: str,
        idempotency_key: str,
    ) -> tuple[str, bool]:
        with self._lock:
            key = (tenant_id, idempotency_key)
            if idempotency_key and key in self._by_key:
                return self._by_key[key], False
            self._seq += 1
            ticket_id = f"TCK-{self._seq:04d}"
            self._tickets[ticket_id] = {
                "ticket_id": ticket_id,
                "tenant_id": tenant_id,
                "subject": subject,
                "description": description,
                "priority": priority,
                "status": "open",
            }
            if idempotency_key:
                self._by_key[key] = ticket_id
            return ticket_id, True

    def list(self, *, tenant_id: str, limit: int) -> list[dict[str, Any]]:
        with self._lock:
            items = [
                dict(ticket)
                for ticket in self._tickets.values()
                if ticket["tenant_id"] == tenant_id
            ]
        return items[:limit]


class TicketCreateTool:
    """工单创建：写工具，强制人工审批，幂等键去重。"""

    spec = ToolSpec(
        name="ticket_create",
        description="创建客服工单；写操作，需要人工审批，支持幂等键去重。",
        input_schema={
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "description": {"type": "string"},
                "priority": {"type": "string", "enum": sorted(_PRIORITIES)},
                "idempotency_key": {"type": "string"},
            },
            "required": ["subject"],
            "additionalProperties": False,
        },
        risk_level="write",
        scopes=["tool:write"],
        timeout_s=10,
        idempotent=True,
        requires_approval=True,
    )

    def __init__(self, store: TicketStore) -> None:
        self._store = store

    async def invoke(self, args: dict, ctx: AuthContext) -> ToolResult:
        started = time.monotonic()
        priority = str(args.get("priority") or "normal")
        if priority not in _PRIORITIES:
            return ToolResult(
                tool=self.spec.name,
                ok=False,
                error="TOOL_INVALID_ARGS",
                latency_ms=_elapsed_ms(started),
            )
        ticket_id, created = self._store.create(
            tenant_id=ctx.tenant_id,
            subject=str(args.get("subject") or ""),
            description=str(args.get("description") or ""),
            priority=priority,
            idempotency_key=str(args.get("idempotency_key") or ""),
        )
        output = json.dumps(
            {"ticket_id": ticket_id, "created": created, "status": "open"},
            ensure_ascii=False,
        )
        return ToolResult(
            tool=self.spec.name, ok=True, output=output, latency_ms=_elapsed_ms(started)
        )


class TicketListTool:
    """工单列表：只读。"""

    spec = ToolSpec(
        name="ticket_list",
        description="列出租户下的工单；只读。",
        input_schema={
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
            "additionalProperties": False,
        },
        risk_level="read",
        scopes=["tool:read"],
        timeout_s=10,
        idempotent=True,
    )

    def __init__(self, store: TicketStore) -> None:
        self._store = store

    async def invoke(self, args: dict, ctx: AuthContext) -> ToolResult:
        started = time.monotonic()
        limit = max(1, min(int(args.get("limit") or 20), 100))
        items = self._store.list(tenant_id=ctx.tenant_id, limit=limit)
        return ToolResult(
            tool=self.spec.name,
            ok=True,
            output=json.dumps(items, ensure_ascii=False),
            latency_ms=_elapsed_ms(started),
        )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
