from __future__ import annotations

from zcaisagent.domain.models import Message
from zcaisagent.ports.llm import LLMPort, LLMRequest, LLMResponse

_SYSTEM = (
    "你是检索查询改写器。基于对话历史，把用户最新问题改写成一条独立、完整、"
    "可检索的中文查询。只输出改写后的查询，不要回答、不要解释、不要引号。"
)
_MAX_HISTORY = 4
_MAX_HISTORY_CHARS = 200
_MAX_REWRITE_CHARS = 200
_QUOTE_PAIRS = (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"))


def rewrite_request(
    goal: str, messages: list[Message], timeout_s: float = 30.0, max_tokens: int = 128
) -> LLMRequest:
    """构造多轮查询改写请求；调用方复用同一请求做轨迹留痕。"""
    history = [
        f"{message.role}: {message.content.strip()[:_MAX_HISTORY_CHARS]}"
        for message in messages[:-1][-_MAX_HISTORY:]
        if message.content.strip()
    ]
    history.append(f"最新问题：{goal}")
    return LLMRequest(
        messages=[
            Message(role="system", content=_SYSTEM),
            Message(role="user", content="\n".join(history)),
        ],
        purpose="rewrite",
        max_tokens=max_tokens,
        timeout_s=timeout_s,
    )


def _strip_quotes(text: str) -> str:
    """去掉首尾空白与成对引号，不破坏查询内部标点。"""
    cleaned = text.strip()
    for left, right in _QUOTE_PAIRS:
        if len(cleaned) >= 2 and cleaned[0] == left and cleaned[-1] == right:
            return cleaned[1:-1].strip()
    return cleaned


async def rewrite_query_with_llm(
    llm: LLMPort,
    goal: str,
    messages: list[Message],
    timeout_s: float = 30.0,
    max_tokens: int = 128,
) -> LLMResponse | None:
    """LLM 兜底改写多轮查询；异常、超时、空串或超长结果返回 None。"""
    request = rewrite_request(goal, messages, timeout_s, max_tokens)
    try:
        response = await llm.complete(request)
    except Exception:  # noqa: BLE001 - 查询改写失败不中断主链路
        return None
    text = _strip_quotes(response.text)
    if not text or len(text) > _MAX_REWRITE_CHARS:
        return None
    return response.model_copy(update={"text": text})
