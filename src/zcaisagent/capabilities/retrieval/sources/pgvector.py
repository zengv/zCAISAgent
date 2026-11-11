import time

from zcaisagent.domain.enums import SourceType
from zcaisagent.domain.models import Evidence
from zcaisagent.ports.llm import EmbeddingPort
from zcaisagent.ports.retrieval import RetrieveRequest, RetrieveResult
from zcaisagent.ports.security import AuthContext
from zcaisagent.ports.store import VectorStorePort


class PgVectorRetriever:
    """pgvector 检索源；不可用时降级为空结果，不阻断其他源。"""

    source_name = "pgvector"
    source_type = SourceType.DOCUMENT

    def __init__(self, store: VectorStorePort, embedder: EmbeddingPort) -> None:
        self.store = store
        self.embedder = embedder

    async def retrieve(
        self, req: RetrieveRequest, ctx: AuthContext
    ) -> RetrieveResult:
        started = time.perf_counter()
        try:
            vectors = await self.embedder.embed([req.query])
            hits = self.store.search(
                vectors[0], req.tenant_id, req.acl_tokens, req.top_k
            )
        except Exception:  # noqa: BLE001 - 单源不可用不阻断融合
            return RetrieveResult(
                source_name=self.source_name,
                degraded=True,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        evidence = [
            Evidence(
                id=chunk.id,
                source_type=SourceType.DOCUMENT,
                source_name=self.source_name,
                title=chunk.title,
                uri=chunk.uri,
                content=chunk.content,
                snippet=chunk.content[:160],
                score=round(score, 4),
                relevance=round(score, 4),
                authority="secondary",
                version=chunk.version,
                metadata={"doc_id": chunk.doc_id, "index": chunk.index},
                parent_content=chunk.parent_content,
            )
            for chunk, score in hits
        ]
        return RetrieveResult(
            source_name=self.source_name,
            evidence=evidence,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
