import re

from zcaisagent.capabilities.conversation.context import is_topic_switch
from zcaisagent.domain.models import Message

# 候选主体切分：并列连接词与「分别/以及」，即上一轮可能涉及多个主体。
_SPLIT_PATTERN = re.compile(r"以及|分别|[和及与、；;]")
# 逐段清洗时去掉的问句/属性词，保留主体名词。
_NOISE_WORDS = (
    "是多少",
    "多少",
    "几天",
    "多久",
    "几个",
    "怎么",
    "如何",
    "哪些",
    "什么",
    "是否",
    "请问",
    "呢",
    "吗",
    "？",
    "?",
    "。",
    "，",
    ",",
    "标准",
    "流程",
    "规定",
    "政策",
    "要求",
    "分别",
    "以及",
    "和",
    "及",
    "与",
    "、",
    "；",
    ";",
)
_GREETINGS = ("你好", "您好", "hi", "hello", "谢谢", "thanks")
_PRONOUN_MARKERS = ("它", "这个", "那个", "该", "其", "上面", "刚才", "上述")
_SHORT_GOAL = 8
_MIN_SUBJECT = 2
_MAX_SUBJECT = 12
_MAX_CANDIDATES = 4
_DEFAULT_QUESTION = "您问的是哪一项？"


def _strip_noise(text: str) -> str:
    cleaned = text
    for word in _NOISE_WORDS:
        cleaned = cleaned.replace(word, "")
    return cleaned.strip()


def _candidates(messages: list[Message]) -> list[str]:
    """从最近一条历史用户消息中抽取候选主体；无历史则没有候选。"""
    previous = [
        message.content.strip()
        for message in messages[:-1]
        if message.role == "user" and message.content.strip()
    ]
    if not previous:
        return []
    result: list[str] = []
    for segment in _SPLIT_PATTERN.split(previous[-1]):
        subject = _strip_noise(segment)
        if _MIN_SUBJECT <= len(subject) <= _MAX_SUBJECT and subject not in result:
            result.append(subject)
    return result[:_MAX_CANDIDATES]


def _is_greeting(text: str) -> bool:
    lowered = text.strip().lower()
    return len(lowered) <= 6 and any(word in lowered for word in _GREETINGS)


def _is_under_specified(text: str, candidates: list[str]) -> bool:
    """本轮问句是否指代不清：过短、含单数指代词，或去掉问句词后自身没有主体。"""
    # 已点明候选主体（如「病假呢？」）说明指代清晰，不再追问。
    if any(item in text for item in candidates):
        return False
    if len(text) <= _SHORT_GOAL:
        return True
    if any(marker in text for marker in _PRONOUN_MARKERS):
        return True
    # 自行携带主体的完整问句（如「出差住宿标准是多少？」）不追问。
    cleaned = _strip_noise(text)
    return len(cleaned) <= 2 and not any(item in cleaned for item in candidates)


def _model_clarify(model_hint: dict | None) -> dict | None:
    """规则未命中时采纳模型判断；选项必须为 2-4 个非空字符串。"""
    if not isinstance(model_hint, dict) or not model_hint.get("needs_clarify"):
        return None
    raw_options = model_hint.get("clarify_options")
    if not isinstance(raw_options, list):
        return None
    options: list[str] = []
    for item in raw_options:
        if isinstance(item, str) and item.strip() and item.strip() not in options:
            options.append(item.strip())
    if not 2 <= len(options) <= 4:
        return None
    raw_question = model_hint.get("clarify_question")
    question = (
        raw_question.strip()
        if isinstance(raw_question, str) and raw_question.strip()
        else _DEFAULT_QUESTION
    )
    return {"questions": [{"question": question, "options": options}], "reason": "model"}


def detect_clarify(
    goal: str, messages: list[Message], model_hint: dict | None = None
) -> dict | None:
    """判断本轮问题是否需要先澄清；纯函数，不依赖上下文与 IO。

    保守策略：只有存在多个历史候选主体、且本轮问句指代不清时才追问；
    显式换话题、寒暄与自洽问句都不追问。规则命中优先于模型判断。
    """
    text = goal.strip()
    if not text:
        return None

    candidates = _candidates(messages)
    if (
        len(candidates) >= 2
        and not is_topic_switch(text)
        and not _is_greeting(text)
        and _is_under_specified(text, candidates)
    ):
        options = list(dict.fromkeys([*candidates[:3], "其他"]))[:_MAX_CANDIDATES]
        if len(options) >= 2:
            return {
                "questions": [{"question": _DEFAULT_QUESTION, "options": options}],
                "reason": "ambiguous_followup",
            }

    return _model_clarify(model_hint)
