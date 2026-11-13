from pathlib import Path

from zcaisagent.domain.models import Chunk
from zcaisagent.domain.versions import split_document_id
from zcaisagent.ingestion.chunker import chunk_document
from zcaisagent.ingestion.parsers import parse_file


def _title(path: Path, text: str) -> str:
    for line in text.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return path.stem


def ingest_directory(
    directory: Path,
    tenant_id: str = "demo",
    acl: list[str] | None = None,
    parent_max_chars: int | None = None,
    acl_by_file: dict[str, list[str]] | None = None,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    if not directory.exists():
        return chunks
    for path in sorted(directory.rglob("*")):
        if path.suffix.lower() not in {".md", ".txt", ".markdown", ".pdf"}:
            continue
        try:
            text = parse_file(path)
        except Exception:  # noqa: BLE001 - one bad file must not break ingest
            continue
        if not text.strip():
            continue
        # 同类文档的多个版本共用族名作为 doc_id，版本号单独记录，供检索取最新版。
        doc_id, version = split_document_id(path.stem)
        relative = path.relative_to(directory).as_posix()
        file_acl = (acl_by_file or {}).get(relative) or (acl_by_file or {}).get(path.name)
        chunks.extend(
            chunk_document(
                doc_id=doc_id,
                title=_title(path, text),
                text=text,
                tenant_id=tenant_id,
                acl=file_acl or acl,
                uri=str(path),
                version=version,
                parent_max_chars=parent_max_chars,
            )
        )
    return chunks
