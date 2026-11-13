import re
import sqlite3
import time

from zcaisagent.capabilities.retrieval.sources.inmemory import current_versions
from zcaisagent.domain.enums import SourceType
from zcaisagent.domain.models import Chunk, Evidence
from zcaisagent.domain.versions import split_document_id
from zcaisagent.ports.retrieval import RetrieveRequest, RetrieveResult
from zcaisagent.ports.security import AuthContext

_CJK = re.compile(r"[\u4e00-\u9fff]")
_LATIN = re.compile(r"[a-zA-Z0-9_]+")


def _terms(text: str) -> list[str]:
    """中文按单字 + 二元组展开，与 domain.text 的切分口径保持一致。

    FTS5 的 unicode61 分词器不会切分中文，因此入库与查询都先展开成空格分隔的
    词串，才能让"调休"这类两字词命中。
    """
    lowered = text.lower()
    terms = _LATIN.findall(lowered)
    chars = _CJK.findall(lowered)
    terms.extend(chars)
    terms.extend(chars[index] + chars[index + 1] for index in range(len(chars) - 1))
    return terms


def _match_query(text: str) -> str:
    # token 只含中英文与数字，再加引号包裹，规避 FTS5 查询语法字符。
    return " OR ".join(f'"{term}"' for term in dict.fromkeys(_terms(text)))


def _acl_column(tokens: list[str]) -> str:
    return "|" + "|".join(dict.fromkeys(tokens)) + "|"


class SqliteBm25Retriever:
    """基于 SQLite FTS5 的 BM25 检索源；索引常驻内存，零外部依赖。"""

    source_name = "bm25"
    source_type = SourceType.DOCUMENT

    def __init__(self) -> None:
        self.chunks: list[Chunk] = []
        self._db = sqlite3.connect(":memory:", check_same_thread=False)
        self._db.execute(
            """
            CREATE VIRTUAL TABLE chunks USING fts5(
                chunk_id UNINDEXED,
                doc_id UNINDEXED,
                title UNINDEXED,
                content UNINDEXED,
                uri UNINDEXED,
                tenant_id UNINDEXED,
                acl UNINDEXED,
                chunk_index UNINDEXED,
                version UNINDEXED,
                parent_content UNINDEXED,
                terms,
                tokenize = 'unicode61'
            )
            """
        )

    def add(self, chunks: list[Chunk]) -> None:
        if not chunks:
            return
        self.chunks.extend(chunks)
        self._db.executemany(
            "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    chunk.id,
                    chunk.doc_id,
                    chunk.title,
                    chunk.content,
                    chunk.uri or "",
                    chunk.tenant_id,
                    _acl_column(chunk.acl),
                    str(chunk.index),
                    chunk.version or "",
                    chunk.parent_content or "",
                    " ".join(_terms(f"{chunk.title} {chunk.content}")),
                )
                for chunk in chunks
            ],
        )
        self._db.commit()

    def __len__(self) -> int:
        return len(self.chunks)

    async def retrieve(self, req: RetrieveRequest, ctx: AuthContext) -> RetrieveResult:
        started = time.perf_counter()
        match = _match_query(req.query)
        sql = (
            "SELECT chunk_id, doc_id, title, content, uri, chunk_index, version, "
            "parent_content, bm25(chunks) AS rank FROM chunks "
            "WHERE terms MATCH ? AND tenant_id = ?"
        )
        params: list[object] = [match, req.tenant_id]
        if req.acl_tokens:
            sql += " AND (" + " OR ".join("acl LIKE ?" for _ in req.acl_tokens) + ")"
            params.extend(f"%|{token}|%" for token in req.acl_tokens)
        else:
            # 无任何 ACL token 时按拒绝处理，避免空 token 被解释为“不过滤”。
            sql += " AND 1 = 0"
        # 版本过滤进入查询条件，先过滤再打分，避免旧版本占用候选名额。
        # doc_id 是文档族，需与 version 组合才能区分同一文档的新旧版本。
        active = sorted(
            {f"{chunk.doc_id}|{chunk.version or ''}" for chunk in current_versions(self.chunks)}
        )
        if active:
            sql += " AND (doc_id || '|' || version) IN (" + ",".join("?" for _ in active) + ")"
            params.extend(active)
        sql += " ORDER BY rank LIMIT ?"
        params.append(max(req.top_k, 0))

        evidence: list[Evidence] = []
        if match and req.top_k > 0:
            for rank, row in enumerate(self._db.execute(sql, params)):
                # FTS5 的 bm25 在小语料上会退化为 0，展示分改用名次分，
                # 与 RRF 按名次融合的口径一致。
                score = round(1.0 / (1.0 + rank), 4)
                _, parsed = split_document_id(row[1])
                evidence.append(
                    Evidence(
                        id=row[0],
                        source_type=SourceType.DOCUMENT,
                        source_name=self.source_name,
                        title=row[2],
                        uri=row[4] or None,
                        content=row[3],
                        snippet=row[3][:160],
                        score=score,
                        relevance=score,
                        authority="secondary",
                        version=row[6] or parsed,
                        metadata={"doc_id": row[1], "index": int(row[5])},
                        parent_content=row[7] or None,
                    )
                )
        return RetrieveResult(
            source_name=self.source_name,
            evidence=evidence,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
