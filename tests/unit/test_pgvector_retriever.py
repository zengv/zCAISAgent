import asyncio

from zcaisagent.capabilities.retrieval.sources.pgvector import PgVectorRetriever
from zcaisagent.ports.retrieval import RetrieveRequest
from tests.conftest import make_auth


class _BrokenStore:
    def search(self, *args, **kwargs):
        raise RuntimeError("database down")

    def ensure_schema(self) -> None:
        return None

    def upsert(self, chunks, vectors) -> int:
        return 0

    def load_chunks(self, tenant_id=None):
        return []


class _FakeEmbedder:
    async def embed(self, texts):
        return [[0.1, 0.2] for _ in texts]


def test_retriever_degrades_when_store_fails() -> None:
    retriever = PgVectorRetriever(_BrokenStore(), _FakeEmbedder())
    result = asyncio.run(
        retriever.retrieve(
            RetrieveRequest(query="年假", tenant_id="demo", acl_tokens=["public"]),
            make_auth(),
        )
    )

    assert result.degraded
    assert result.evidence == []
