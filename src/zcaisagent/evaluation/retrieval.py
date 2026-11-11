from __future__ import annotations

import math
import re
from typing import Any

from zcaisagent.capabilities.retrieval.fusion import rrf_fuse, source_weight
from zcaisagent.capabilities.retrieval.sources.inmemory import current_versions
from zcaisagent.domain.models import Chunk
from zcaisagent.domain.versions import split_document_id
from zcaisagent.ports.retrieval import RetrieverPort, RetrieveRequest
from zcaisagent.ports.security import AuthContext

_PUNCT = re.compile(r"[\s，。、；：！？,.;:!?¥￥()（）\"'“”‘’]")
_DEFAULT_KS = (5, 10)


def _normalize(text: str) -> str:
    return _PUNCT.sub("", text).lower()


def _answers(case: dict[str, Any]) -> list[str]:
    values = [case.get("expected_answer"), *(case.get("expected_answers") or [])]
    return [_normalize(str(value)) for value in values if value]


def answer_relevant_ids(case: dict[str, Any], chunks: list[Chunk]) -> set[str]:
    """以答案字符串字面命中作为 chunk 级相关标注；无字面答案的样本不参与。

    若样本带 expected_sources，则只在期望来源文档内匹配，避免答案数字恰好
    出现在无关文档中造成假标注。
    """
    answers = _answers(case)
    if not answers:
        return set()
    sources = {str(item) for item in (case.get("expected_sources") or [])}
    return {
        chunk.id
        for chunk in current_versions(chunks)
        if (not sources or split_document_id(chunk.doc_id)[0] in sources)
        and any(answer in _normalize(chunk.content) for answer in answers)
    }


def doc_relevant_ids(case: dict[str, Any], chunks: list[Chunk]) -> set[str]:
    """以 expected_sources 对应文档的当前版本 chunk 作为文档级相关标注。"""
    sources = {str(item) for item in (case.get("expected_sources") or [])}
    if not sources:
        return set()
    return {
        chunk.id
        for chunk in current_versions(chunks)
        if split_document_id(chunk.doc_id)[0] in sources
    }


def recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float | None:
    if not relevant:
        return None
    return len(set(ranked[:k]) & relevant) / len(relevant)


def mrr_at_k(ranked: list[str], relevant: set[str], k: int) -> float | None:
    if not relevant:
        return None
    for rank, item in enumerate(ranked[:k], start=1):
        if item in relevant:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(ranked: list[str], relevant: set[str], k: int) -> float | None:
    if not relevant:
        return None
    dcg = sum(
        1.0 / math.log2(rank + 1)
        for rank, item in enumerate(ranked[:k], start=1)
        if item in relevant
    )
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(len(relevant), k) + 1))
    return dcg / ideal if ideal else None


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _aggregate(records: list[dict[str, Any]], keys: tuple[int, ...]) -> dict[str, Any]:
    report: dict[str, Any] = {"cases": len(records)}
    for key in keys:
        report[f"recall@{key}"] = _mean(
            [item[f"recall@{key}"] for item in records if item[f"recall@{key}"] is not None]
        )
    report["mrr"] = _mean([item["mrr"] for item in records if item["mrr"] is not None])
    report["ndcg@10"] = _mean(
        [item["ndcg@10"] for item in records if item["ndcg@10"] is not None]
    )
    return report


async def _rank(
    retrievers: list[RetrieverPort],
    case: dict[str, Any],
    top_k: int,
    reranker=None,
    hyde=None,
) -> list[str]:
    request = RetrieveRequest(
        query=str(case["question"]),
        top_k=top_k,
        tenant_id=str(case.get("tenant") or "demo"),
        acl_tokens=[str(token) for token in (case.get("acl") or ["public"])],
    )
    auth = AuthContext(
        tenant_id=request.tenant_id,
        user_id="eval",
        acl_tokens=request.acl_tokens,
    )
    # 原始 query 永远先检索；HyDE 可用时追加一轮假设性答案召回。
    queries = [request.query]
    if hyde is not None:
        hypo = await hyde(request.query)
        if hypo:
            queries.append(hypo)
    ranked_lists: list[list] = []
    weights: list[float] = []
    for query in queries:
        query_request = request.model_copy(update={"query": query})
        for retriever in retrievers:
            ranked_lists.append(
                (await retriever.retrieve(query_request, auth)).evidence
            )
            weights.append(source_weight(retriever.source_name))
    fused = (
        rrf_fuse(ranked_lists, weights=weights)
        if len(ranked_lists) > 1
        else (ranked_lists[0] if ranked_lists else [])
    )
    if reranker is not None and fused:
        # LLM 重排：按相关性分数降序（稳定排序保留同分原序）；失败回退 RRF 原序。
        try:
            scores = await reranker.rerank(request.query, [item.content for item in fused])
            fused = [
                item
                for _, item in sorted(
                    zip(scores, fused), key=lambda pair: -pair[0]
                )
            ]
        except Exception:  # noqa: BLE001 - rerank 不可用时保持融合原序
            pass
    return [item.id for item in fused[:top_k]]


async def run_retrieval_eval(
    retrievers: list[RetrieverPort],
    chunks: list[Chunk],
    cases: list[dict[str, Any]],
    top_k: int = 10,
    config_name: str = "fused",
    reranker=None,
    hyde=None,
) -> dict[str, Any]:
    """离线检索评测：不调用 LLM，只衡量召回与排序质量；reranker/hyde 可选。"""
    keys = tuple(key for key in _DEFAULT_KS if key <= top_k) or (top_k,)
    doc_records: list[dict[str, Any]] = []
    answer_records: list[dict[str, Any]] = []
    for case in cases:
        ranked = await _rank(retrievers, case, top_k, reranker=reranker, hyde=hyde)
        for target, records in (
            (doc_relevant_ids(case, chunks), doc_records),
            (answer_relevant_ids(case, chunks), answer_records),
        ):
            if not target:
                continue
            record: dict[str, Any] = {"id": case.get("id")}
            for key in keys:
                record[f"recall@{key}"] = recall_at_k(ranked, target, key)
            record["mrr"] = mrr_at_k(ranked, target, top_k)
            record["ndcg@10"] = ndcg_at_k(ranked, target, min(10, top_k))
            records.append(record)
    return {
        "config": config_name,
        "sources": [retriever.source_name for retriever in retrievers],
        "top_k": top_k,
        "total_cases": len(cases),
        "doc": _aggregate(doc_records, keys),
        "answer": _aggregate(answer_records, keys),
    }
