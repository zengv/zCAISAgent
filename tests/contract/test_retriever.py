import asyncio

from zcaisagent.ingestion.chunker import chunk_document
from zcaisagent.ingestion.pipeline import ingest_directory
from zcaisagent.ports.retrieval import RetrieveRequest
from tests.conftest import make_chunk, make_ctx


def test_retriever_filters_acl() -> None:
    ctx = make_ctx([
        make_chunk("finance#0", "财务部工资表", acl=["finance"]),
        make_chunk("public#0", "差旅住宿标准 800 元", acl=["public"]),
    ])
    result = asyncio.run(
        ctx.retriever.retrieve(
            RetrieveRequest(query="工资表", tenant_id="demo", acl_tokens=["public"]),
            ctx.auth,
        )
    )
    assert all(item.metadata.get("doc_id") != "finance" for item in result.evidence)


def test_chunker_produces_chunks() -> None:
    chunks = chunk_document("doc", "标题", "第一段内容。\n\n第二段内容。")
    assert len(chunks) >= 1
    assert chunks[0].id.startswith("doc#")


def test_ingestion_tags_family_and_version(tmp_path) -> None:
    (tmp_path / "travel_policy-v1.0.md").write_text(
        "# 差旅\n\n一线城市住宿标准为 801 元/晚。", encoding="utf-8"
    )
    (tmp_path / "travel_policy-v1.1.md").write_text(
        "# 差旅\n\n一线城市住宿标准为 800.1 元/晚。", encoding="utf-8"
    )
    chunks = ingest_directory(tmp_path)
    assert {chunk.doc_id for chunk in chunks} == {"travel_policy"}
    assert {chunk.version for chunk in chunks} == {"1.0", "1.1"}


def test_retriever_returns_only_latest_document_version() -> None:
    ctx = make_ctx(
        [
            make_chunk("travel_policy-v1.0#0", "一线城市住宿标准为 801 元/晚"),
            make_chunk("travel_policy-v1.1#0", "一线城市住宿标准为 800.1 元/晚"),
        ]
    )
    result = asyncio.run(
        ctx.retriever.retrieve(
            RetrieveRequest(
                query="一线城市住宿标准", tenant_id="demo", acl_tokens=["public"]
            ),
            ctx.auth,
        )
    )
    assert [item.id for item in result.evidence] == ["travel_policy-v1.1#0"]
    assert result.evidence[0].version == "1.1"
