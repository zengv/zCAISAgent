import asyncio

from zcaisagent.adapters.cache.llm import CachedLLM
from zcaisagent.adapters.cache.memory import InMemoryCache
from zcaisagent.domain.models import Message
from zcaisagent.ports.llm import LLMDelta, LLMRequest, LLMResponse


class CountingLLM:
    """记录调用次数的确定性 LLM 桩。"""

    def __init__(self) -> None:
        self.complete_calls = 0
        self.stream_calls = 0
        self.model = "counting"

    async def complete(self, req: LLMRequest) -> LLMResponse:
        self.complete_calls += 1
        return LLMResponse(
            text="answer",
            tokens_in=10,
            tokens_out=2,
            cost_usd=0.5,
            model="counting",
        )

    def stream(self, req: LLMRequest):
        self.stream_calls += 1

        async def gen():
            yield LLMDelta(text="a")
            yield LLMDelta(text="", final=LLMResponse(text="a", tokens_in=3))

        return gen()


def _request(
    temperature: float = 0.0, purpose: str = "synthesize", max_tokens: int = 64
) -> LLMRequest:
    return LLMRequest(
        messages=[Message(role="user", content="你好")],
        temperature=temperature,
        purpose=purpose,
        max_tokens=max_tokens,
    )


def test_second_identical_request_hits_cache() -> None:
    inner = CountingLLM()
    cache = InMemoryCache()
    llm = CachedLLM(inner, cache, ttl_s=60)

    async def flow():
        return await llm.complete(_request()), await llm.complete(_request())

    first, second = asyncio.run(flow())

    assert inner.complete_calls == 1
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert second.text == "answer"
    assert second.tokens_in == 0
    assert second.tokens_out == 0
    assert second.cost_usd == 0.0
    assert second.cached_tokens == 10
    assert cache.stats().hits == 1


def test_non_zero_temperature_is_not_cached() -> None:
    inner = CountingLLM()
    cache = InMemoryCache()
    llm = CachedLLM(inner, cache)

    async def flow():
        await llm.complete(_request(temperature=0.2))
        await llm.complete(_request(temperature=0.2))

    asyncio.run(flow())

    assert inner.complete_calls == 2
    assert cache.stats().hits == 0


def test_stream_passes_through_without_caching() -> None:
    inner = CountingLLM()
    cache = InMemoryCache()
    llm = CachedLLM(inner, cache)

    async def flow():
        first = [delta async for delta in llm.stream(_request())]
        second = [delta async for delta in llm.stream(_request())]
        return first, second

    first, second = asyncio.run(flow())

    assert inner.stream_calls == 2
    assert inner.complete_calls == 0
    assert [delta.text for delta in first] == [delta.text for delta in second]
    assert cache.stats().hits == 0
    assert cache.stats().misses == 0


def test_cache_key_includes_purpose_and_max_tokens() -> None:
    inner = CountingLLM()
    llm = CachedLLM(inner, InMemoryCache())

    async def flow():
        await llm.complete(_request(purpose="analyze"))
        await llm.complete(_request(purpose="grade"))
        await llm.complete(_request(max_tokens=128))

    asyncio.run(flow())

    assert inner.complete_calls == 3
