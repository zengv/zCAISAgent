"""零依赖 YAML 子集 frontmatter 解析（用于 skills/*/SKILL.md）。

支持：`key: value` 标量（引号/布尔/整数/浮点）、内联列表 `key: [a, b]`、
块列表 `key:` 后跟 `- item`。复杂嵌套 YAML 不支持（保持零依赖）。
"""
from __future__ import annotations

from typing import Any


def _scalar(value: str) -> Any:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    lowered = text.lower()
    if lowered in {"true", "yes"}:
        return True
    if lowered in {"false", "no"}:
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """把 `---` 包裹的 frontmatter 拆成 (meta, body)；无 frontmatter 返回 ({}, 原文)。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        raise ValueError("frontmatter not closed with '---'")
    meta_lines = lines[1:end]
    body = "\n".join(lines[end + 1 :])
    data: dict[str, Any] = {}
    index = 0
    while index < len(meta_lines):
        raw = meta_lines[index]
        if not raw.strip() or raw.lstrip().startswith("#"):
            index += 1
            continue
        if raw[:1] in {" ", "\t"}:
            raise ValueError(f"unexpected indent: {raw!r}")
        if ":" not in raw:
            raise ValueError(f"invalid frontmatter line: {raw!r}")
        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip()
        if value == "":
            items: list[Any] = []
            index += 1
            while index < len(meta_lines) and meta_lines[index].lstrip().startswith("- "):
                items.append(_scalar(meta_lines[index].lstrip()[2:]))
                index += 1
            data[key] = items
            continue
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            data[key] = [_scalar(part) for part in inner.split(",")] if inner else []
        else:
            data[key] = _scalar(value)
        index += 1
    return data, body
