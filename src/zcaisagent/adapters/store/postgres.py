from __future__ import annotations

from typing import Any

from zcaisagent.capabilities.retrieval.sources.inmemory import current_versions
from zcaisagent.domain.models import Chunk

_CHUNK_COLUMNS = (
    "id, doc_id, title, content, chunk_index, tenant_id, acl, uri, version, "
    "parent_content, parent_id"
)


class StoreUnavailable(Exception):
    """连接/驱动不可用；调用方应回退内存实现。"""


def schema_sql(dimensions: int) -> str:
    return (
        "CREATE EXTENSION IF NOT EXISTS vector;"
        "CREATE TABLE IF NOT EXISTS chunks ("
        "id text PRIMARY KEY,"
        "doc_id text NOT NULL,"
        "title text NOT NULL,"
        "content text NOT NULL,"
        "chunk_index integer NOT NULL DEFAULT 0,"
        "tenant_id text NOT NULL,"
        "acl text[] NOT NULL DEFAULT '{}',"
        "uri text,"
        "version text,"
        "published_at timestamptz,"
        f"embedding vector({dimensions}) NOT NULL,"
        "parent_content text,"
        "parent_id text"
        ");"
        "CREATE INDEX IF NOT EXISTS chunks_tenant_idx ON chunks (tenant_id);"
        "CREATE INDEX IF NOT EXISTS chunks_embedding_idx ON chunks "
        "USING hnsw (embedding vector_cosine_ops);"
    )


def upsert_sql() -> str:
    return (
        "INSERT INTO chunks (id, doc_id, title, content, chunk_index, tenant_id, acl, "
        "uri, version, parent_content, parent_id, embedding) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector) "
        "ON CONFLICT (id) DO UPDATE SET "
        "content = EXCLUDED.content, tenant_id = EXCLUDED.tenant_id, "
        "acl = EXCLUDED.acl, version = EXCLUDED.version, "
        "parent_content = EXCLUDED.parent_content, parent_id = EXCLUDED.parent_id, "
        "embedding = EXCLUDED.embedding"
    )


def search_sql() -> str:
    return (
        "SELECT " + _CHUNK_COLUMNS + ", embedding <=> %s::vector AS distance "
        "FROM chunks WHERE tenant_id = %s AND acl && %s::text[] "
        "ORDER BY distance LIMIT %s"
    )


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{value:.6f}" for value in vector) + "]"


def _row_to_chunk(row: tuple[Any, ...]) -> Chunk:
    return Chunk(
        id=row[0],
        doc_id=row[1],
        title=row[2],
        content=row[3],
        index=int(row[4]),
        tenant_id=row[5],
        acl=list(row[6] or []),
        uri=row[7],
        version=row[8],
        parent_content=row[9],
        parent_id=row[10],
    )


class PsycopgVectorStore:
    """pgvector 实现；psycopg 延迟导入，未安装时不影响内存路径。"""

    def __init__(self, dsn: str, dimensions: int = 1024) -> None:
        self.dsn = dsn
        self.dimensions = dimensions
        self._conn: Any = None

    def _connect(self):
        if self._conn is not None:
            return self._conn
        if not self.dsn:
            raise StoreUnavailable("database dsn is empty")
        try:
            import psycopg
        except ImportError as exc:
            raise StoreUnavailable("psycopg is not installed") from exc
        try:
            self._conn = psycopg.connect(self.dsn)
        except Exception as exc:  # noqa: BLE001 - 统一转成可供回退的领域异常
            raise StoreUnavailable(str(exc)) from exc
        return self._conn

    def ensure_schema(self) -> None:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(schema_sql(self.dimensions))
        conn.commit()

    def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> int:
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors length mismatch")
        rows = [
            (
                chunk.id,
                chunk.doc_id,
                chunk.title,
                chunk.content,
                chunk.index,
                chunk.tenant_id,
                chunk.acl,
                chunk.uri,
                chunk.version,
                chunk.parent_content,
                chunk.parent_id,
                vector_literal(vector),
            )
            for chunk, vector in zip(chunks, vectors)
        ]
        if not rows:
            return 0
        conn = self._connect()
        with conn.cursor() as cur:
            cur.executemany(upsert_sql(), rows)
        conn.commit()
        return len(rows)

    def load_chunks(self, tenant_id: str | None = None) -> list[Chunk]:
        sql = f"SELECT {_CHUNK_COLUMNS} FROM chunks"
        params: tuple[Any, ...] = ()
        if tenant_id:
            sql += " WHERE tenant_id = %s"
            params = (tenant_id,)
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [_row_to_chunk(row) for row in rows]

    def search(
        self,
        vector: list[float],
        tenant_id: str,
        acl_tokens: list[str],
        top_k: int,
    ) -> list[tuple[Chunk, float]]:
        if top_k <= 0:
            return []
        # 版本过滤在 Python 侧完成，先多取候选再截断。
        limit = max(top_k * 3, top_k)
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                search_sql(),
                (vector_literal(vector), tenant_id, list(acl_tokens), limit),
            )
            rows = cur.fetchall()
        chunks = [_row_to_chunk(row[:11]) for row in rows]
        distances = {row[0]: float(row[11]) for row in rows}
        kept = current_versions(chunks)
        kept.sort(key=lambda chunk: distances[chunk.id])
        return [
            (chunk, round(1.0 - distances[chunk.id], 4)) for chunk in kept[:top_k]
        ]
