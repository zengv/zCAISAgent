from __future__ import annotations

import re

from zcaisagent.domain.models import Evidence

_WHITESPACE = re.compile(r"\s+")


def _content_key(evidence: Evidence) -> str:
    # 内容相同即合并为一条，跨源重复不再保留多份；来源出处记入 metadata.sources。
    return _WHITESPACE.sub("", evidence.content)[:300]


def rrf_fuse(ranked_lists: list[list[Evidence]], k: int = 60) -> list[Evidence]:
    """Reciprocal Rank Fusion：按名次融合多路检索结果，并按内容去重合并来源。"""
    scores: dict[str, float] = {}
    best: dict[str, Evidence] = {}
    for ranked in ranked_lists:
        for rank, item in enumerate(ranked, start=1):
            scores[item.id] = scores.get(item.id, 0.0) + 1.0 / (k + rank)
            current = best.get(item.id)
            if current is None or item.score >= current.score:
                best[item.id] = item

    merged: dict[str, dict] = {}
    for evidence_id, _ in sorted(scores.items(), key=lambda pair: pair[1], reverse=True):
        item = best[evidence_id]
        key = _content_key(item)
        entry = merged.get(key)
        if entry is None:
            entry = {"item": item, "sources": []}
            merged[key] = entry
        record = {"source_name": item.source_name, "evidence_id": item.id}
        if record not in entry["sources"]:
            entry["sources"].append(record)

    ordered: list[Evidence] = []
    for entry in merged.values():
        item = entry["item"].model_copy(deep=True)
        metadata = dict(item.metadata)
        metadata["sources"] = entry["sources"]
        ordered.append(item.model_copy(update={"metadata": metadata}))
    return ordered
