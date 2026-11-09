from zcaisagent.capabilities.conversation.context import resolve_followup
from zcaisagent.domain.models import Message


def _messages(*contents: str) -> list[Message]:
    return [Message(role="user", content=content) for content in contents]


def test_short_followup_resolves_from_previous_user_message() -> None:
    messages = _messages("年假天数是多少？", "它呢？")
    assert resolve_followup("它呢？", messages) == "年假天数是多少？ 它呢？"


def test_pronoun_followup_resolves_even_when_long() -> None:
    messages = _messages("住宿标准是多少？", "这个标准对二线城市也适用吗？")
    query = resolve_followup("这个标准对二线城市也适用吗？", messages)
    assert query == "住宿标准是多少？ 这个标准对二线城市也适用吗？"


def test_self_contained_question_keeps_original() -> None:
    messages = _messages("年假天数是多少？", "出差住宿标准是多少？")
    assert resolve_followup("出差住宿标准是多少？", messages) is None


def test_topic_switch_does_not_inherit_context() -> None:
    messages = _messages("年假天数是多少？", "换个话题：加班怎么申请？")
    assert resolve_followup("换个话题：加班怎么申请？", messages) is None


def test_first_turn_has_no_context_to_resolve() -> None:
    assert resolve_followup("它呢？", _messages("它呢？")) is None
