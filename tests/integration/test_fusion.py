from zcaisagent.capabilities.retrieval.fusion import rrf_fuse
from zcaisagent.domain.models import Evidence


def make_evidence(evidence_id: str, content: str, source: str = "kb") -> Evidence:
    return Evidence(
        id=evidence_id,
        source_name=source,
        content=content,
        score=0.5,
        relevance=0.5,
    )


def test_rrf_prefers_evidence_ranked_in_multiple_lists() -> None:
    only_once = make_evidence("b#0", "仅一路命中")
    both_lists = make_evidence("a#0", "两路都命中")
    fused = rrf_fuse([[both_lists], [both_lists, only_once]])
    assert [item.id for item in fused] == ["a#0", "b#0"]


def test_rrf_dedupes_same_source_content_across_queries() -> None:
    first = make_evidence("a#0", "一线城市住宿标准 800 元")
    second = make_evidence("a#1", "一线城市住宿标准 800 元")
    fused = rrf_fuse([[first], [second]])
    assert len(fused) == 1


def test_rrf_merges_cross_source_duplicates_and_keeps_provenance() -> None:
    kb = make_evidence("kb#0", "一线城市住宿标准 800 元", source="kb")
    web = make_evidence("web#0", "一线城市住宿标准 800 元", source="web")
    fused = rrf_fuse([[kb], [web]])
    assert len(fused) == 1
    assert fused[0].metadata["sources"] == [
        {"source_name": "kb", "evidence_id": "kb#0"},
        {"source_name": "web", "evidence_id": "web#0"},
    ]
