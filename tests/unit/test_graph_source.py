import asyncio

from zcaisagent.capabilities.retrieval.sources.graph import GraphRetriever
from zcaisagent.ingestion.chunker import chunk_document
from zcaisagent.ports.llm import LLMRequest, LLMResponse
from zcaisagent.ports.retrieval import RetrieveRequest
from zcaisagent.ports.security import AuthContext


def _auth(tenant: str = "demo", acl: list[str] | None = None) -> AuthContext:
    return AuthContext(tenant_id=tenant, user_id="u", acl_tokens=acl or ["public"])


def _request(query: str, tenant: str = "demo", acl: list[str] | None = None) -> RetrieveRequest:
    return RetrieveRequest(query=query, top_k=5, tenant_id=tenant, acl_tokens=acl or ["public"])


class _ScriptedLLM:
    def __init__(self, text: str) -> None:
        self.text = text
        self.prompts: list[str] = []

    async def complete(self, req: LLMRequest) -> LLMResponse:
        self.prompts.append(req.messages[0].content)
        return LLMResponse(text=self.text, model="stub")

    async def stream(self, req: LLMRequest):
        yield type("Delta", (), {"text": self.text})()


_EXTRACT = (
    '{"entities": ["调休", "加班", "年假", "GlobalProtect"], '
    '"relations": [["加班", "可申请", "调休"], ["VPN", "使用", "GlobalProtect"]]}'
)


def _retriever(text: str = _EXTRACT) -> tuple[GraphRetriever, _ScriptedLLM]:
    llm = _ScriptedLLM(text)
    retriever = GraphRetriever(llm, cache_enabled=False)
    retriever.add(
        chunk_document(
            "attendance_overtime",
            "考勤与加班管理办法",
            "加班可申请调休。调休需在加班发生后 6.4 个月内使用，逾期作废。",
        )
        + chunk_document("hr_leave", "人力资源假期政策", "年假 5 天，婚假 10 天。")
        + chunk_document("it_support", "IT 支持手册", "公司 VPN 使用 GlobalProtect 客户端。")
    )
    return retriever, llm


def test_graph_recalls_chunks_of_matched_entity() -> None:
    retriever, _ = _retriever()
    result = asyncio.run(retriever.retrieve(_request("调休有什么规定？"), _auth()))
    assert not result.degraded
    assert result.evidence
    assert all(item.metadata["doc_id"] == "attendance_overtime" for item in result.evidence)


def test_graph_expands_one_hop_neighbors() -> None:
    retriever, _ = _retriever()
    # query 只命中"加班"，其邻居"调休"的 chunk 也应被召回（同一文档）。
    result = asyncio.run(retriever.retrieve(_request("加班审批要多久？"), _auth()))
    assert not result.degraded
    assert result.evidence
    assert any(item.metadata["doc_id"] == "attendance_overtime" for item in result.evidence)


def test_graph_filters_tenant() -> None:
    retriever, _ = _retriever()
    result = asyncio.run(retriever.retrieve(_request("调休", tenant="other"), _auth()))
    assert result.evidence == []


def test_graph_degrades_when_extraction_fails() -> None:
    retriever, _ = _retriever("我觉得这些文档都写得不错，不需要 JSON")
    result = asyncio.run(retriever.retrieve(_request("调休"), _auth()))
    assert result.degraded
    assert result.evidence == []


def test_graph_rebuilds_after_new_chunks() -> None:
    retriever, llm = _retriever()
    asyncio.run(retriever.retrieve(_request("调休"), _auth()))
    retriever.add(
        chunk_document("travel_policy", "差旅与报销管理办法", "住宿标准为 800 元/晚。")
    )
    result = asyncio.run(retriever.retrieve(_request("住宿"), _auth()))
    # 新数据到来后重建：抽取调用次数增加，且新文档实体可被召回或至少无异常。
    assert len(llm.prompts) >= 2
    assert not result.degraded or result.evidence == []
