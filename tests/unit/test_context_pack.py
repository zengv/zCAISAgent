from zcaisagent.capabilities.retrieval.context_pack import estimate_tokens, pack
from zcaisagent.domain.models import Evidence


def _evidence(
    evidence_id: str,
    content: str,
    score: float = 0.5,
    grade: str = "keep",
    parent: str | None = None,
) -> Evidence:
    return Evidence(
        id=evidence_id,
        source_name="test",
        content=content,
        score=score,
        final_score=score,
        grade=grade,
        parent_content=parent,
    )


def test_pack_prefers_parent_content() -> None:
    packed = pack([_evidence("e1", "子块", parent="父块正文")], max_tokens=100, max_items=5)

    assert len(packed) == 1
    assert packed[0].content == "父块正文"


def test_pack_drops_low_grade_and_respects_budget() -> None:
    items = [
        _evidence("e1", "甲" * 10, score=0.9),
        _evidence("e2", "乙" * 10, score=0.8),
        _evidence("e3", "丙", score=0.1, grade="drop"),
    ]

    packed = pack(items, max_tokens=12, max_items=5)

    assert [item.id for item in packed] == ["e1"]
    assert estimate_tokens("甲" * 10) == 10


def test_pack_keeps_at_least_one_item() -> None:
    packed = pack([_evidence("e1", "甲" * 100)], max_tokens=5, max_items=5)

    assert [item.id for item in packed] == ["e1"]
