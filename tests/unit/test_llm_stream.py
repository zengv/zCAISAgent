import asyncio
import json

import httpx

from zcaisagent.adapters.llm import openai_compat
from zcaisagent.adapters.llm.fake import FakeLLM
from zcaisagent.adapters.llm.openai_compat import OpenAICompatLLM
from zcaisagent.domain.models import Message
from zcaisagent.ports.llm import LLMRequest
from zcaisagent.settings import ModelProvider, Settings


def _request() -> LLMRequest:
    return LLMRequest(messages=[Message(role="user", content="你好")], max_tokens=64)


def test_fake_stream_chunks_and_final() -> None:
    async def collect():
        return [delta async for delta in FakeLLM().stream(_request())]

    deltas = asyncio.run(collect())
    assert len(deltas) >= 2
    assert "".join(delta.text for delta in deltas).strip()
    assert all(delta.final is None for delta in deltas[:-1])
    final = deltas[-1]
    assert final.text == ""
    assert final.final is not None
    assert final.final.tokens_in > 0
    assert final.final.tokens_out > 0


def test_openai_compat_stream_parses_sse(monkeypatch) -> None:
    lines = [
        {"model": "test-model", "choices": [{"delta": {"content": "你"}}]},
        {"model": "test-model", "choices": [{"delta": {"content": "好"}}]},
        {
            "model": "test-model",
            "choices": [],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "prompt_tokens_details": {"cached_tokens": 3},
            },
        },
    ]
    body = "".join(f"data: {json.dumps(line)}\n\n" for line in lines)
    body += "data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["stream"] is True
        return httpx.Response(
            200,
            content=body.encode("utf-8"),
            headers={"content-type": "text/event-stream"},
        )

    original_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(openai_compat.httpx, "AsyncClient", client_factory)

    provider = ModelProvider(
        id="test",
        name="test",
        model="test-model",
        base_url="https://example.test/v1",
        api_key="secret",
        context_window=1000,
        reasoning_effort="low",
    )
    settings = Settings(model_provider="test", model_providers={"test": provider})
    llm = OpenAICompatLLM(settings=settings)

    async def collect():
        return [delta async for delta in llm.stream(_request())]

    deltas = asyncio.run(collect())
    assert [delta.text for delta in deltas[:-1]] == ["你", "好"]
    final = deltas[-1]
    assert final.text == ""
    assert final.final is not None
    assert final.final.tokens_in == 10
    assert final.final.tokens_out == 2
    assert final.final.cached_tokens == 3
    assert final.final.model == "test-model"
