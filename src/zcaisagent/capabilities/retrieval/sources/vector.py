import math
import time

from zcaisagent.capabilities.retrieval.sources.inmemory import current_versions
from zcaisagent.domain.enums import SourceType
from zcaisagent.domain.models import Chunk, Evidence
from zcaisagent.domain.versions import split_document_id
from zcaisagent.ports.llm import EmbeddingPort
from zcaisagent.ports.retrieval import RetrieveRequest, RetrieveResult
from zcaisagent.ports.security import AuthContext


def cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    norm_l = math.sqrt(sum(value * value for value in left))
    norm_r = math.sqrt(sum(value * value for value in right))
    if norm_l == 0.0 or norm_r == 0.0:
        return 0.0
    return dot / (norm_l * norm_r)


class VectorRetriever:
    """内存余弦向量检索源；chunk 向量懒构建，embedding 服务不可用时降级。"""

    source_name = "vector"
    source_type = SourceType.DOCUMENT

    def __init__(self, embedder: EmbeddingPort) -> None:
        self.embedder = embedder
        self.chunks: list[Chunk] = []
        self._vectors: dict[str, list[float]] | None = None
        self._failed = False

    def add(self, chunks: list[Chunk]) -> None:
        if not chunks:
            return
        self.chunks.extend(chunks)
        # 新数据到来后重建向量。
        self._vectors = None

    def __len__(self) -> int:
        return len(self.chunks)

    async def _ensure_vectors(self) -> None:
        if self._vectors is not None or self._failed:
            return
        texts = [f"{chunk.title} {chunk.content}" for chunk in self.chunks]
        vectors = await self.embedder.embed(texts)
        if len(vectors) != len(self.chunks):
            self._failed = True
            raise ValueError("embedding count mismatch")
        self._vectors = {
            chunk.id: vector
            for chunk, vector in zip(self.chunks, vectors, strict=False)
        }

    async def retrieve(self, req: RetrieveRequest, ctx: AuthContext) -> RetrieveResult:
        started = time.perf_counter()
        try:
            await self._ensure_vectors()
            query_vector = (await self.embedder.embed([req.query]))[0]
        except Exception:  # noqa: BLE001 - embedding 服务不可用时降级返回
            self._failed = True
            return RetrieveResult(source_name=self.source_name, degraded=True)

        acl = set(req.acl_tokens)
        candidates = current_versions(
            [
                chunk
                for chunk in self.chunks
                if chunk.tenant_id == req.tenant_id
                and (not chunk.acl or bool(set(chunk.acl) & acl))
            ]
        )
        scored = [
            (cosine(query_vector, self._vectors[chunk.id]), chunk)
            for chunk in candidates
        ]
        scored = [(score, chunk) for score, chunk in scored if score > 0]
        scored.sort(key=lambda item: item[0], reverse=True)

        evidence = []
        for score, chunk in scored[: req.top_k]:
            _, parsed = split_document_id(chunk.doc_id)
            evidence.append(
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
                    version=chunk.version or parsed,
                    metadata={"doc_id": chunk.doc_id, "index": chunk.index},
                    parent_content=chunk.parent_content,
                )
            )
        return RetrieveResult(
            source_name=self.source_name,
            evidence=evidence,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
