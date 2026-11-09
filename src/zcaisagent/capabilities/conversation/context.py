from zcaisagent.domain.models import Message

_SWITCH_MARKERS = ("换个话题", "另一个问题", "新问题", "另外问", "换个问题", "重新开始")
_PRONOUN_MARKERS = (
    "它",
    "它们",
    "这个",
    "那个",
    "该",
    "其",
    "上述",
    "上面",
    "刚才",
    "这些",
    "那些",
)
_ELLIPSIS_MARKERS = ("呢", "还有", "前面", "刚才")
_SHORT_GOAL = 6


def is_topic_switch(goal: str) -> bool:
    text = goal.strip()
    return any(marker in text for marker in _SWITCH_MARKERS)


def resolve_followup(goal: str, messages: list[Message]) -> str | None:
    """基于最近一轮用户输入补全指代/省略，返回检索用 query。

    原始 goal 不改写；仅在短问句或含指代词时启用补全，显式切换话题时不继承上下文。
    """
    text = goal.strip()
    if not text or is_topic_switch(text):
        return None
    previous = [
        message.content.strip()
        for message in messages[:-1]
        if message.role == "user" and message.content.strip()
    ]
    if not previous:
        return None
    if len(text) <= _SHORT_GOAL or any(
        marker in text for marker in (*_PRONOUN_MARKERS, *_ELLIPSIS_MARKERS)
    ):
        return f"{previous[-1]} {text}"
    return None
