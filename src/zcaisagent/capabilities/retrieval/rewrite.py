from __future__ import annotations

import re

_QUESTION_WORDS = (
    "请问",
    "是什么",
    "是多少",
    "什么时候",
    "怎么样",
    "怎么",
    "如何",
    "哪些",
    "吗",
    "呢",
    "？",
    "?",
)
_SPLIT = re.compile(r"[和及与、；;]|以及")


def _light(query: str) -> str:
    cleaned = query
    for word in _QUESTION_WORDS:
        cleaned = cleaned.replace(word, " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or query


def rewrite_queries(goal: str, complexity: str, intent: str = "查事实") -> list[dict[str, str]]:
    """确定性查询改写分级：原 query 永远排第一并保留。"""
    original = {"strategy": "none", "query": goal}
    if complexity == "simple":
        if len(goal) <= 12 or intent in {"闲聊", "不安全"}:
            return [original]
        light = _light(goal)
        if light == goal:
            return [original]
        return [original, {"strategy": "light", "query": light}]

    variants = [original]
    light = _light(goal)
    if light != goal:
        variants.append({"strategy": "light", "query": light})
    if complexity == "multi_hop":
        parts = [part.strip() for part in _SPLIT.split(goal) if part.strip()]
        for index, part in enumerate(parts[:3], start=1):
            if part != goal:
                variants.append({"strategy": f"multi_{index}", "query": _light(part)})
    else:
        # 开放问题：按标点拆分为子查询，最多 3 条，保持可回指原目标。
        parts = [
            part.strip(" ，。？！,?!")
            for part in re.split(r"[，。？！,?!]", goal)
            if len(part.strip(" ，。？！,?!")) >= 4
        ]
        for index, part in enumerate(parts[:3], start=1):
            if part != goal:
                variants.append({"strategy": f"decompose_{index}", "query": part})
    return variants
