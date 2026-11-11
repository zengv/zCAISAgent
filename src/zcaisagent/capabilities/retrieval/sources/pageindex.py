import re
import time
from dataclasses import dataclass
from pathlib import Path

from zcaisagent.capabilities.retrieval.sources.inmemory import current_versions
from zcaisagent.domain.enums import SourceType
from zcaisagent.domain.models import Chunk, Evidence
from zcaisagent.domain.text import overlap_ratio
from zcaisagent.domain.versions import split_document_id
from zcaisagent.ports.retrieval import RetrieveRequest, RetrieveResult
from zcaisagent.ports.security import AuthContext

_ANCHOR_CHARS = 30


@dataclass
class _Section:
    """文档目录树的一个章节节点（PageIndex）。"""

    doc_id: str
    version: str | None
    title: str
    content: str
    tenant_id: str
    acl: list[str]
    chunk_id: str | None = None


def _parse_sections(text: str) -> list[tuple[str, str]]:
    """把 markdown 拆成 (章节标题, 章节正文)；`#` 为文档名，`##` 起始新章节。"""
    sections: list[tuple[str, list[str]]] = []
    current_title: str | None = None
    for block in text.split("\n\n"):
        block = block.strip()
        if not block or block.startswith("<!--") or block.startswith("# "):
            continue
        if block.startswith("## "):
            current_title = block[3:].strip()
            sections.append((current_title, []))
            continue
        if current_title is not None:
            sections[-1][1].append(block)
    return [(title, "\n\n".join(body)) for title, body in sections if body]


def _norm_head(text: str) -> str:
    return re.sub(r"\s+", "", text)[:_ANCHOR_CHARS]


class PageIndexRetriever:
    """文档目录树检索源（PageIndex）：

    通过 chunk.uri 回读原始 markdown 构建章节树，章节标题与正文聚合打分
    天然优于 chunk 切分边界；命中章节锚定回 chunk id 以兼容引用与评测。
    """

    source_name = "pageindex"
    source_type = SourceType.DOCUMENT

    def __init__(self) -> None:
        self.chunks: list[Chunk] = []
        self._sections: list[_Section] = []
        self._built = False
        self._failed = False

    def add(self, chunks: list[Chunk]) -> None:
        if not chunks:
            return
        self.chunks.extend(chunks)
        self._built = False

    def __len__(self) -> int:
        return len(self._sections)

    def _ensure_tree(self) -> None:
        if self._built or self._failed:
            return
        # 当前版本 chunk 的 uri 集合（同一文档只处理一次）。
        live = current_versions(self.chunks)
        uris: dict[str, Chunk] = {}
        for chunk in live:
            if chunk.uri:
                uris.setdefault(chunk.uri, chunk)
        for uri, sample in uris.items():
            try:
                text = Path(uri).read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001 - 文件不可读时跳过该文档
                continue
            _, parsed_version = split_document_id(sample.doc_id)
            # chunker 丢弃了标题行，用去空白正文前缀把章节锚定回 chunk。
            doc_chunks = [
                chunk for chunk in live if chunk.uri == uri
            ]
            packed = {
                chunk.id: re.sub(r"\s+", "", chunk.content) for chunk in doc_chunks
            }
            for title, body in _parse_sections(text):
                anchor = _norm_head(body)
                chunk_id = next(
                    (
                        chunk.id
                        for chunk in doc_chunks
                        if anchor and anchor in packed[chunk.id]
                    ),
                    None,
                )
                if chunk_id is None:
                    continue
                self._sections.append(
                    _Section(
                        doc_id=sample.doc_id,
                        version=sample.version or parsed_version,
                        title=title,
                        content=body,
                        tenant_id=sample.tenant_id,
                        acl=list(sample.acl),
                        chunk_id=chunk_id,
                    )
                )
        if not self._sections:
            self._failed = True
            raise RuntimeError("pageindex: no sections built")
        self._built = True

    async def retrieve(self, req: RetrieveRequest, ctx: AuthContext) -> RetrieveResult:
        started = time.perf_counter()
        try:
            self._ensure_tree()
        except Exception:  # noqa: BLE001 - 树构建不可用时降级返回
            return RetrieveResult(source_name=self.source_name, degraded=True)

        acl = set(req.acl_tokens)
        candidates = [
            section
            for section in self._sections
            if section.tenant_id == req.tenant_id
            and (not section.acl or bool(set(section.acl) & acl))
        ]
        scored = [
            (overlap_ratio(req.query, f"{section.title} {section.content}"), section)
            for section in candidates
        ]
        scored = [(score, section) for score, section in scored if score > 0]
        scored.sort(key=lambda item: item[0], reverse=True)

        by_id = {chunk.id: chunk for chunk in self.chunks}
        evidence: list[Evidence] = []
        seen_chunks: set[str] = set()
        for score, section in scored:
            if len(evidence) >= req.top_k or req.top_k <= 0:
                break
            # 多个短章节可能锚定同一 chunk，保留得分最高的一条。
            if section.chunk_id in seen_chunks:
                continue
            seen_chunks.add(section.chunk_id)
            chunk = by_id.get(section.chunk_id)
            if chunk is None:
                continue
            _, parsed = split_document_id(chunk.doc_id)
            evidence.append(
                Evidence(
                    id=chunk.id,
                    source_type=SourceType.DOCUMENT,
                    source_name=self.source_name,
                    title=chunk.title,
                    uri=chunk.uri,
                    # 章节标题聚合进正文，为引用校验提供完整上下文。
                    content=f"{section.title}：{section.content}",
                    snippet=section.content[:160],
                    score=round(score, 4),
                    relevance=round(score, 4),
                    authority="secondary",
                    version=chunk.version or parsed,
                    metadata={
                        "doc_id": chunk.doc_id,
                        "index": chunk.index,
                        "section": section.title,
                    },
                    parent_content=chunk.parent_content,
                )
            )
        return RetrieveResult(
            source_name=self.source_name,
            evidence=evidence,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
