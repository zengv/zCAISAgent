from __future__ import annotations

from zcaisagent.ports.retrieval import RetrieverPort, RetrieveRequest
from zcaisagent.ports.security import AuthContext
from zcaisagent.ports.tools import ToolResult, ToolSpec


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
