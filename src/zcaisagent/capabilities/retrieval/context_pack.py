from __future__ import annotations

from zcaisagent.domain.models import Evidence


def estimate_tokens(text: str) -> int:
    """粗估 token：中文按 1 字符 1 token，ASCII 按 4 字符 1 token。"""
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    return (len(text) - ascii_chars) + (ascii_chars + 3) // 4


def pack(
    evidence: list[Evidence], max_tokens: int, max_items: int
) -> list[Evidence]:
    """按可信度打包证据：父文替换正文，按条数与 token 预算截断。"""
    ranked = sorted(
        evidence, key=lambda item: (item.final_score, item.score), reverse=True
    )
    kept = [item for item in ranked if item.grade != "drop"]
    if max_items > 0:
        kept = kept[:max_items]

    packed: list[Evidence] = []
    used = 0
    for item in kept:
        content = item.parent_content or item.content
        cost = estimate_tokens(content)
        # 至少保留 1 条，即使首条就超预算（否则合成阶段无资料可用）。
        if packed and used + cost > max_tokens:
            break
        packed.append(item.model_copy(update={"content": content}))
        used += cost
    return packed
