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
    # 可选：指定 reasoning_effort 等级，覆盖默认 provider 的选择。
    reasoning_effort: Effort | None = None
    # 思考型模型（DeepSeek 等）对结构化抽取任务会无限思考耗尽 max_tokens，
    # 置 True 时适配器注入 chat_template_kwargs 关闭思考。
    disable_thinking: bool = False


class LLMResponse(BaseModel):
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    # prompt 中命中服务端前缀缓存的 token 数（OpenAI / DeepSeek 兼容字段）。
    cached_tokens: int = 0
    # 推理模型的 thinking token 会计入 completion_tokens，但不出现在 text 中；
    # 单独暴露用于排查"max_tokens 够但 content 为空"的情况。
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    model: str = ""


class LLMDelta(BaseModel):
    text: str
    # 流式最后一块携带完整用量（tokens/cost），供调用方一次性记账。
    final: LLMResponse | None = None


class LLMPort(Protocol):
    async def complete(self, req: LLMRequest) -> LLMResponse: ...

    def stream(self, req: LLMRequest) -> AsyncIterator[LLMDelta]: ...


class EmbeddingPort(Protocol):
    """文本向量化；实现须保证同文本同向量、向量长度一致。"""

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class RerankPort(Protocol):
    """查询-文档相关性重排；不可用（解析失败/超时）时抛异常由调用方回退。"""

    async def rerank(self, query: str, documents: list[str]) -> list[float]: ...
