import time

from zcaisagent.domain.enums import SourceType
from zcaisagent.domain.models import Chunk, Evidence
from zcaisagent.domain.text import overlap_ratio
from zcaisagent.domain.versions import split_document_id, version_key
from zcaisagent.ports.retrieval import RetrieveRequest, RetrieveResult
from zcaisagent.ports.security import AuthContext


def _family_and_version(chunk: Chunk) -> tuple[str, str | None]:
    family, parsed = split_document_id(chunk.doc_id)
    return family, chunk.version or parsed


def current_versions(chunks: list[Chunk]) -> list[Chunk]:
    """同一文档族只保留最高版本，避免新旧版本混合召回；无版本号的文档原样保留。"""
    latest: dict[str, tuple[int, ...]] = {}
    for chunk in chunks:
        family, version = _family_and_version(chunk)
        key = version_key(version) if version else None
        if key is not None and (family not in latest or key > latest[family]):
            latest[family] = key
    if not latest:
        return chunks
    kept: list[Chunk] = []
    for chunk in chunks:
        family, version = _family_and_version(chunk)
        key = version_key(version) if version else None
        if key is None or key == latest.get(family):
            kept.append(chunk)
    return kept


class InMemoryRetriever:
    source_name = "inmemory"
    source_type = SourceType.DOCUMENT

    def __init__(self) -> None:
        self.chunks: list[Chunk] = []

    def add(self, chunks: list[Chunk]) -> None:
        self.chunks.extend(chunks)

    def __len__(self) -> int:
        return len(self.chunks)

    async def retrieve(
        self, req: RetrieveRequest, ctx: AuthContext
    ) -> RetrieveResult:
        started = time.perf_counter()
        acl = set(req.acl_tokens)
        candidates = [
            chunk
            for chunk in self.chunks
            if chunk.tenant_id == req.tenant_id
            and (not chunk.acl or bool(set(chunk.acl) & acl))
        ]
        # 版本过滤先于打分召回：默认只检索当前生效版本。
        candidates = current_versions(candidates)
        scored = [
            (overlap_ratio(req.query, f"{chunk.title} {chunk.content}"), chunk)
            for chunk in candidates
        ]
        scored = [(score, chunk) for score, chunk in scored if score > 0]
        scored.sort(key=lambda item: item[0], reverse=True)
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
                version=_family_and_version(chunk)[1],
                metadata={"doc_id": chunk.doc_id, "index": chunk.index},
                parent_content=chunk.parent_content,
            )
            for score, chunk in scored[: req.top_k]
        ]
        return RetrieveResult(
            source_name=self.source_name,
            evidence=evidence,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
