import re

_CJK = re.compile(r"[\u4e00-\u9fff]")
_LATIN = re.compile(r"[a-zA-Z0-9_]+")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def numbers_in(text: str) -> set[str]:
    return set(_NUMBER.findall(text))


def tokenize(text: str) -> set[str]:
    text = text.lower()
    latin = set(_LATIN.findall(text))
    chars = _CJK.findall(text)
    bigrams = {chars[i] + chars[i + 1] for i in range(len(chars) - 1)}
    return latin | set(chars) | bigrams


def content_tokens(text: str) -> set[str]:
    """用于 faithfulness 的内容词元：仅保留英文/数字词与中日韩二元组。

    单字在中文里过于容易与证据重合，二元组更能反映"语义片段是否出现"。
    """
    text = text.lower()
    latin = set(_LATIN.findall(text))
    chars = _CJK.findall(text)
    bigrams = {chars[i] + chars[i + 1] for i in range(len(chars) - 1)}
    return latin | bigrams


def overlap_ratio(left: str, right: str) -> float:
    left_tokens = tokenize(left)
    if not left_tokens:
        return 0.0
    right_tokens = tokenize(right)
    return len(left_tokens & right_tokens) / len(left_tokens)
