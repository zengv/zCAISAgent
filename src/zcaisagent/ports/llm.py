from collections.abc import AsyncIterator
from typing import Literal, Protocol

from pydantic import BaseModel

from zcaisagent.domain.models import Message

Effort = Literal["low", "high", "max"]


class LLMRequest(BaseModel):
    messages: list[Message]
    model: str | None = None
    temperature: float = 0.0
    max_tokens: int = 1024
    purpose: str = "synthesize"
    response_format: dict | None = None
    # 单次请求超时；None 时由适配器的默认超时决定。长输出等级（L3）需更长等待。
    timeout_s: float | None = None
    # 可选：强制指定 reasoning_effort，覆盖 purpose 映射。
    reasoning_effort: Effort | None = None


class LLMResponse(BaseModel):
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    # 推理模型的 thinking token 会计入 completion_tokens，但不出现在 text 中；
    # 单独暴露用于排查"max_tokens 够但 content 为空"的情况。
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""


class LLMDelta(BaseModel):
    text: str


class LLMPort(Protocol):
    async def complete(self, req: LLMRequest) -> LLMResponse: ...

    def stream(self, req: LLMRequest) -> AsyncIterator[LLMDelta]: ...
