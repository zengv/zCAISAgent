import os

import pytest

from zcaisagent.adapters.store.postgres import PsycopgVectorStore
from zcaisagent.domain.models import Chunk

DSN = os.getenv("ZCAISAGENT_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DSN, reason="ZCAISAGENT_TEST_DATABASE_URL not set"
)


def _chunk(chunk_id: str) -> Chunk:
    return Chunk(
        id=chunk_id,
        doc_id="doc",
        title="测试",
        content=f"内容 {chunk_id}",
        index=0,
        tenant_id="demo",
        acl=["public"],
    )


def test_upsert_load_and_search_roundtrip() -> None:
    store = PsycopgVectorStore(DSN, 3)
    store.ensure_schema()

    chunks = [_chunk("doc#0"), _chunk("doc#1")]
    written = store.upsert(chunks, [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    assert written == 2

    # 重新建立连接，验证数据已持久化。
    reopened = PsycopgVectorStore(DSN, 3)
    loaded = {chunk.id for chunk in reopened.load_chunks("demo")}
    assert {"doc#0", "doc#1"} <= loaded

    hits = reopened.search([1.0, 0.0, 0.0], "demo", ["public"], 2)
    assert hits
    assert hits[0][0].id == "doc#0"
