import asyncio

import pytest

from zcaisagent.adapters.llm.fake import FakeEmbedding
from zcaisagent.adapters.store.postgres import PsycopgVectorStore, search_sql
from zcaisagent.capabilities.retrieval.sources.bm25 import SqliteBm25Retriever
from zcaisagent.capabilities.retrieval.sources.graph import GraphRetriever
from zcaisagent.capabilities.retrieval.sources.inmemory import InMemoryRetriever
from zcaisagent.capabilities.retrieval.sources.pageindex import PageIndexRetriever
from zcaisagent.capabilities.retrieval.sources.vector import VectorRetriever
from zcaisagent.ingestion.chunker import chunk_document
from zcaisagent.ports.llm import LLMRequest, LLMResponse
from zcaisagent.ports.retrieval import RetrieveRequest
from zcaisagent.ports.security import AuthContext

_SOURCES = ["inmemory", "bm25", "vector", "pageindex", "graph"]
_FINANCE_ACL = ["finance"]
_CONTENT = "调休需在加班发生后 6 个月内使用，逾期作废。"
_DOC = f"# 财务制度\n\n## 调休\n\n{_CONTENT}\n"


class _ScriptedLLM:
    def __init__(self, text: str) -> None:
        self.text = text

    async def complete(self, req: LLMRequest) -> LLMResponse:
        return LLMResponse(text=self.text, model="stub")

    async def stream(self, req: LLMRequest):
        yield type("Delta", (), {"text": self.text})()


def _build(source: str, tmp_path):
    if source == "inmemory":
        retriever = InMemoryRetriever()
        retriever.add(chunk_document("finance_doc", "财务制度", _CONTENT, acl=_FINANCE_ACL))
        return retriever
    if source == "bm25":
        retriever = SqliteBm25Retriever()
        retriever.add(chunk_document("finance_doc", "财务制度", _CONTENT, acl=_FINANCE_ACL))
        return retriever
    if source == "vector":
        retriever = VectorRetriever(FakeEmbedding())
        retriever.add(chunk_document("finance_doc", "财务制度", _CONTENT, acl=_FINANCE_ACL))
        return retriever
    if source == "pageindex":
        doc = tmp_path / "finance.md"
        doc.write_text(_DOC, encoding="utf-8")
        retriever = PageIndexRetriever()
        retriever.add(
            chunk_document(
                "finance_doc", "财务制度", _DOC, acl=_FINANCE_ACL, uri=str(doc)
            )
        )
        return retriever
    retriever = GraphRetriever(
        _ScriptedLLM('{"entities": ["调休"], "relations": []}'), cache_enabled=False
    )
    retriever.add(chunk_document("finance_doc", "财务制度", _CONTENT, acl=_FINANCE_ACL))
    return retriever


def _request(tenant: str, acl: list[str]) -> RetrieveRequest:
    return RetrieveRequest(query="调休有什么规定？", top_k=5, tenant_id=tenant, acl_tokens=acl)


def _auth(tenant: str, acl: list[str]) -> AuthContext:
    return AuthContext(tenant_id=tenant, user_id="alice", acl_tokens=acl)


def _retrieve(retriever, tenant: str, acl: list[str]):
    return asyncio.run(
        retriever.retrieve(_request(tenant, acl), _auth(tenant, acl))
    )


@pytest.mark.parametrize("source", _SOURCES)
def test_acl_mismatch_recalls_nothing(source: str, tmp_path) -> None:
    retriever = _build(source, tmp_path)
    result = _retrieve(retriever, "demo", ["public"])
    assert result.evidence == []


@pytest.mark.parametrize("source", _SOURCES)
def test_cross_tenant_recalls_nothing(source: str, tmp_path) -> None:
    retriever = _build(source, tmp_path)
    result = _retrieve(retriever, "other", _FINANCE_ACL)
    assert result.evidence == []


@pytest.mark.parametrize("source", _SOURCES)
def test_acl_match_recalls_chunk(source: str, tmp_path) -> None:
    retriever = _build(source, tmp_path)
    result = _retrieve(retriever, "demo", _FINANCE_ACL)
    assert result.evidence
    assert all(item.content for item in result.evidence)


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def execute(self, sql: str, params=None) -> None:
        self.executed.append((sql, params))

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _FakeCursor:
        return self._cursor


def test_pgvector_search_sql_filters_tenant_and_acl() -> None:
    sql = search_sql()
    assert "tenant_id = %s" in sql
    assert "acl && %s::text[]" in sql


def test_pgvector_search_binds_vector_tenant_acl_before_limit() -> None:
    cursor = _FakeCursor()
    store = PsycopgVectorStore("dsn", 4)
    store._conn = _FakeConn(cursor)

    store.search([0.1, 0.2, 0.3, 0.4], "demo", ["public"], 2)

    sql, params = cursor.executed[0]
    assert "tenant_id = %s" in sql
    assert params == ("[0.100000,0.200000,0.300000,0.400000]", "demo", ["public"], 6)
