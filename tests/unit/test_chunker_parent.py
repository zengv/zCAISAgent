from zcaisagent.ingestion.chunker import chunk_document


def test_parent_child_chunks_carry_parent_content() -> None:
    text = "第一段内容。" * 20 + "\n\n" + "第二段内容。" * 20
    chunks = chunk_document("doc", "标题", text, max_chars=100, parent_max_chars=200)

    assert chunks
    assert all(chunk.parent_content for chunk in chunks)
    assert all(chunk.parent_id for chunk in chunks)
    assert all(len(chunk.parent_content or "") <= 200 for chunk in chunks)
    # 同一父块的子块共享相同父文与父 id。
    assert len({chunk.parent_id for chunk in chunks}) > 1


def test_parent_child_disabled_by_default() -> None:
    chunks = chunk_document("doc", "标题", "第一段。\n\n第二段。", max_chars=100)

    assert chunks
    assert all(chunk.parent_content is None for chunk in chunks)
    assert all(chunk.parent_id is None for chunk in chunks)
