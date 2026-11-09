from zcaisagent.domain.models import Chunk


def chunk_text(
    text: str, max_chars: int = 600, overlap: int = 80
) -> list[str]:
    paragraphs = [
        p.strip()
        for p in text.split("\n\n")
        if p.strip() and not p.strip().startswith("#")
    ]
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
) -> list[Chunk]:
    acl = acl or ["public"]
    pieces = chunk_text(text, max_chars=max_chars, overlap=overlap)
    # 同一文档族的多个版本共用 doc_id，片段 id 需带版本号避免碰撞。
    id_prefix = f"{doc_id}-v{version}" if version else doc_id
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
