from typing import Protocol

from zcaisagent.domain.models import Chunk


class VectorStorePort(Protocol):
    """向量持久化端口；实现可以是 pgvector 或其他后端。"""

    def ensure_schema(self) -> None: ...

    def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> int: ...

    def load_chunks(self, tenant_id: str | None = None) -> list[Chunk]: ...

    def search(
        self,
        vector: list[float],
        tenant_id: str,
        acl_tokens: list[str],
        top_k: int,
    ) -> list[tuple[Chunk, float]]: ...
