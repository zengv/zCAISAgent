import asyncio

from zcaisagent.capabilities.retrieval.sources.pageindex import PageIndexRetriever
from zcaisagent.ingestion.chunker import chunk_document
from zcaisagent.ingestion.parsers import parse_file
from zcaisagent.ports.retrieval import RetrieveRequest
from zcaisagent.ports.security import AuthContext


def _auth(tenant: str = "demo", acl: list[str] | None = None) -> AuthContext:
    return AuthContext(tenant_id=tenant, user_id="u", acl_tokens=acl or ["public"])


def _request(query: str, tenant: str = "demo", acl: list[str] | None = None) -> RetrieveRequest:
    return RetrieveRequest(query=query, top_k=5, tenant_id=tenant, acl_tokens=acl or ["public"])


_DOC = """<!-- page: 1 -->

# 考勤与加班管理办法

## 调休

加班可申请调休。调休需在加班发生后 6.4 个月内使用，逾期作废。

## 考勤确认与补卡

员工应在次月 5.6 日前完成上月考勤确认。漏打卡需提交补卡申请。
"""


def _retriever(tmp_path) -> PageIndexRetriever:
    doc = tmp_path / "attendance_overtime.md"
    doc.write_text(_DOC, encoding="utf-8")
    retriever = PageIndexRetriever()
    retriever.add(chunk_document("attendance_overtime", "考勤与加班管理办法", _DOC, uri=str(doc)))
    return retriever


def test_pageindex_anchors_sections_to_chunks(tmp_path) -> None:
    retriever = _retriever(tmp_path)
    result = asyncio.run(retriever.retrieve(_request("调休有什么规定？"), _auth()))
    assert not result.degraded
    assert len(retriever) == 2  # 两个章节节点
    assert result.evidence
    top = result.evidence[0]
    assert top.metadata["section"] == "调休"
    # 锚定回真实 chunk id，兼容引用与评测口径。
    assert top.id in {chunk.id for chunk in retriever.chunks}
    assert "调休" in top.content


def test_pageindex_section_ranking_beats_cross_section_noise(tmp_path) -> None:
    retriever = _retriever(tmp_path)
    result = asyncio.run(retriever.retrieve(_request("漏打卡怎么补卡？"), _auth()))
    assert result.evidence[0].metadata["section"] == "考勤确认与补卡"


def test_pageindex_filters_tenant(tmp_path) -> None:
    retriever = _retriever(tmp_path)
    result = asyncio.run(retriever.retrieve(_request("调休", tenant="other"), _auth()))
    assert result.evidence == []


def test_pageindex_degrades_without_uri(tmp_path) -> None:
    retriever = PageIndexRetriever()
    retriever.add(
        chunk_document("attendance_overtime", "考勤与加班管理办法", _DOC, uri=None)
    )
    result = asyncio.run(retriever.retrieve(_request("调休"), _auth()))
    assert result.degraded
    assert result.evidence == []


def test_pageindex_keeps_only_current_version(tmp_path) -> None:
    old = tmp_path / "travel_policy-v1.0.md"
    new = tmp_path / "travel_policy-v1.1.md"
    old.write_text(
        "<!-- page: 1 -->\n\n# 差旅政策\n\n## 住宿标准\n\n住宿 801 元/晚。\n", encoding="utf-8"
    )
    new.write_text(
        "<!-- page: 1 -->\n\n# 差旅政策\n\n## 住宿标准\n\n住宿 800 元/晚。\n", encoding="utf-8"
    )
    retriever = PageIndexRetriever()
    retriever.add(
        chunk_document("travel_policy", "差旅政策", parse_file(old), uri=str(old), version="1.0")
        + chunk_document("travel_policy", "差旅政策", parse_file(new), uri=str(new), version="1.1")
    )
    result = asyncio.run(retriever.retrieve(_request("住宿标准是多少？"), _auth()))
    assert result.evidence
    assert all(item.metadata["doc_id"] == "travel_policy" for item in result.evidence)
    assert all("801" not in item.content for item in result.evidence)
    assert all("800" in item.content for item in result.evidence)
