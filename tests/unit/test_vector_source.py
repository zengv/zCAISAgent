import asyncio

from zcaisagent.adapters.llm.fake import FakeEmbedding
from zcaisagent.capabilities.retrieval.sources.vector import VectorRetriever
from zcaisagent.domain.models import Chunk
from zcaisagent.ingestion.chunker import chunk_document
from zcaisagent.ports.retrieval import RetrieveRequest
from zcaisagent.ports.security import AuthContext


def _auth(tenant: str = "demo", acl: list[str] | None = None) -> AuthContext:
    return AuthContext(tenant_id=tenant, user_id="u", acl_tokens=acl or ["public"])


def _request(query: str, tenant: str = "demo", acl: list[str] | None = None) -> RetrieveRequest:
    return RetrieveRequest(query=query, top_k=5, tenant_id=tenant, acl_tokens=acl or ["public"])


def _chunk(payload: dict, tenant: str = "demo", acl: list[str] | None = None) -> Chunk:
    return Chunk(tenant_id=tenant, acl=acl or ["public"], **payload)


def _retriever() -> VectorRetriever:
    retriever = VectorRetriever(FakeEmbedding())
    retriever.add(
        chunk_document(
            "attendance_overtime",
            "考勤与加班管理办法",
            "加班可申请调休。调休需在加班发生后 6 个月内使用，逾期作废。",
        )
        + chunk_document("hr_leave", "人力资源假期政策", "年假 5 天，婚假 10 天。")
    )
    return retriever


def test_vector_ranks_topical_chunk_first() -> None:
    result = asyncio.run(_retriever().retrieve(_request("调休有什么规定？"), _auth()))
    assert result.evidence
    assert result.evidence[0].metadata["doc_id"] == "attendance_overtime"
    assert 0 < result.evidence[0].score <= 1


def test_vector_filters_tenant_and_acl() -> None:
    retriever = _retriever()
    assert asyncio.run(retriever.retrieve(_request("调休", tenant="other"), _auth())).evidence == []
    assert asyncio.run(
        retriever.retrieve(_request("调休", acl=["hr"]), _auth(acl=["hr"]))
    ).evidence == []


def test_vector_keeps_only_current_document_version() -> None:
    retriever = VectorRetriever(FakeEmbedding())
    retriever.add(
        chunk_document(
            "travel_policy", "差旅与报销管理办法", "住宿标准为 801 元/晚。", version="1.0"
        )
        + chunk_document(
            "travel_policy", "差旅与报销管理办法", "住宿标准为 800 元/晚。", version="1.1"
        )
    )
    result = asyncio.run(retriever.retrieve(_request("住宿标准"), _auth()))
    contents = [item.content for item in result.evidence]
    assert any("800 元/晚" in item for item in contents)
    assert all("801 元/晚" not in item for item in contents)


def test_vector_degrades_when_embedding_fails() -> None:
    class BrokenEmbedding:
        async def embed(self, texts):
            raise RuntimeError("embedding service down")

    retriever = VectorRetriever(BrokenEmbedding())
    retriever.add([_chunk({"id": "c1", "doc_id": "d1", "title": "t", "content": "c", "index": 0})])
    result = asyncio.run(retriever.retrieve(_request("任意"), _auth()))
    assert result.degraded
    assert result.evidence == []
