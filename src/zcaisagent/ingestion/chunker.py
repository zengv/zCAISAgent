import re

from zcaisagent.domain.models import Chunk

_PAGE_MARKER = re.compile(r"^<!--\s*page:\s*(\d+)\s*-->$")


def _split_pages(text: str) -> list[tuple[str | None, list[str]]]:
    """按 <!-- page: N --> 标记切页；标记之前的段落归入页码为 None 的一组。"""
    pages: list[tuple[str | None, list[str]]] = [(None, [])]
    for raw in text.split("\n\n"):
        paragraph = raw.strip()
        if not paragraph or paragraph.startswith("#"):
            continue
        match = _PAGE_MARKER.match(paragraph)
        if match:
            pages.append((match.group(1), []))
            continue
        pages[-1][1].append(paragraph)
    return pages


def _pack_paragraphs(
    paragraphs: list[str], max_chars: int, overlap: int
) -> list[str]:
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            for start in range(0, len(paragraph), max_chars - overlap):
                piece = paragraph[start : start + max_chars]
                if piece.strip():
                    chunks.append(piece.strip())
            current = ""
            continue
        if len(current) + len(paragraph) + 2 <= max_chars:
            current = f"{current}\n\n{paragraph}".strip()
        else:
            if current:
                chunks.append(current)
            current = paragraph
    if current:
        chunks.append(current)
    return chunks


def chunk_text(
    text: str, max_chars: int = 600, overlap: int = 80
) -> list[str]:
    chunks: list[str] = []
    for page, paragraphs in _split_pages(text):
        prefix = f"<!-- page: {page} -->\n\n" if page else ""
        for piece in _pack_paragraphs(paragraphs, max_chars - len(prefix), overlap):
            chunks.append(f"{prefix}{piece}")
    return chunks


def chunk_document(
    doc_id: str,
    title: str,
    text: str,
    tenant_id: str = "demo",
    acl: list[str] | None = None,
    uri: str | None = None,
    max_chars: int = 600,
    overlap: int = 80,
    version: str | None = None,
    parent_max_chars: int | None = None,
) -> list[Chunk]:
    acl = acl or ["public"]
    # 同一文档族的多个版本共用 doc_id，片段 id 需带版本号避免碰撞。
    id_prefix = f"{doc_id}-v{version}" if version else doc_id

    if parent_max_chars:
        # 先按父块上限合并段落，再在父块内切子块；父块本身不入库。
        parents: list[str] = []
        for page, paragraphs in _split_pages(text):
            prefix = f"<!-- page: {page} -->\n\n" if page else ""
            parents.extend(
                f"{prefix}{piece}"
                for piece in _pack_paragraphs(
                    paragraphs, parent_max_chars - len(prefix), 0
                )
            )
        chunks: list[Chunk] = []
        index = 0
        for parent_index, parent_text in enumerate(parents):
            for piece in chunk_text(parent_text, max_chars=max_chars, overlap=overlap):
                chunks.append(
                    Chunk(
                        id=f"{id_prefix}#{index}",
                        doc_id=doc_id,
                        title=title,
                        content=piece,
                        index=index,
                        tenant_id=tenant_id,
                        acl=acl,
                        uri=uri,
                        version=version,
                        parent_id=f"{id_prefix}#p{parent_index}",
                        parent_content=parent_text,
                    )
                )
                index += 1
        return chunks

    pieces = chunk_text(text, max_chars=max_chars, overlap=overlap)
    return [
        Chunk(
            id=f"{id_prefix}#{index}",
            doc_id=doc_id,
            title=title,
            content=piece,
            index=index,
            tenant_id=tenant_id,
            acl=acl,
            uri=uri,
            version=version,
        )
        for index, piece in enumerate(pieces)
    ]
