from pathlib import Path

from zcaisagent.ingestion.pipeline import ingest_directory


def test_acl_by_file_overrides_default(tmp_path: Path) -> None:
    (tmp_path / "it_support.md").write_text("# IT 支持\n\nVPN 连接。", encoding="utf-8")
    (tmp_path / "hr_leave.md").write_text("# 假期政策\n\n年假 5 天。", encoding="utf-8")

    chunks = ingest_directory(
        tmp_path,
        acl=["public"],
        acl_by_file={"it_support.md": ["it_support"]},
    )
    by_doc: dict[str, set[str]] = {}
    for chunk in chunks:
        by_doc.setdefault(chunk.doc_id, set()).update(chunk.acl)

    assert by_doc["it_support"] == {"it_support"}
    assert by_doc["hr_leave"] == {"public"}
