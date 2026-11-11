from zcaisagent.evaluation.retrieval import (
    answer_relevant_ids,
    doc_relevant_ids,
    mrr_at_k,
    ndcg_at_k,
    recall_at_k,
)
from zcaisagent.ingestion.chunker import chunk_document


def test_recall_and_mrr_use_ranked_ids() -> None:
    relevant = {"a#0", "a#1"}
    ranked = ["x#0", "a#1", "a#0"]
    assert recall_at_k(ranked, relevant, 2) == 0.5
    assert recall_at_k(ranked, relevant, 3) == 1.0
    assert mrr_at_k(ranked, relevant, 3) == 0.5
    assert recall_at_k(ranked, set(), 3) is None


def test_ndcg_rewards_higher_rank() -> None:
    relevant = {"a#0"}
    assert ndcg_at_k(["a#0", "x#0"], relevant, 2) == 1.0
    assert ndcg_at_k(["x#0", "a#0"], relevant, 2) < 1.0


def test_answer_annotation_is_limited_to_expected_sources() -> None:
    chunks = chunk_document("travel_policy", "差旅与报销管理办法", "住宿标准为 800 元/晚。")
    chunks += chunk_document("it_support", "IT 支持手册", "设备报销上限 800 元。")
    case = {
        "expected_answer": "800 元",
        "expected_sources": ["travel_policy"],
    }
    assert answer_relevant_ids(case, chunks) == {"travel_policy#0"}


def test_doc_annotation_uses_current_version_only() -> None:
    chunks = chunk_document("policy", "制度", "旧版内容。", version="1.0")
    chunks += chunk_document("policy", "制度", "新版内容。", version="1.1")
    assert doc_relevant_ids({"expected_sources": ["policy"]}, chunks) == {"policy-v1.1#0"}
