"""文档版本标识：从文件名/doc_id 中拆出文档族与版本号，并比较版本高低。"""

import re

_VERSION_SUFFIX = re.compile(
    r"^(?P<family>.+?)[-_. ]?v(?P<version>\d+(?:\.\d+)*)$", re.IGNORECASE
)


def split_document_id(value: str) -> tuple[str, str | None]:
    """把 `travel_policy-v1.1` 拆成 (`travel_policy`, `1.1`)；无版本后缀时版本为 None。"""
    match = _VERSION_SUFFIX.match(value)
    if match is None:
        return value, None
    return match.group("family").rstrip("-_. "), match.group("version")


def version_key(version: str) -> tuple[int, ...] | None:
    """版本号转可比较元组；非纯数字点分版本返回 None。"""
    parts = version.split(".")
    if not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)
