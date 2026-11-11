import asyncio

import httpx
import pytest

from zcaisagent.adapters.llm.openai_compat import OpenAICompatLLM
from zcaisagent.domain.models import Message
from zcaisagent.ports.llm import LLMRequest
from zcaisagent.settings import ModelProvider, Settings

_PRIMARY_HOST = "primary.test"
_BACKUP_HOST = "backup.test"


def _provider(provider_id: str, host: str, model: str) -> ModelProvider:
    return ModelProvider(
        id=provider_id,
        name=provider_id,
        model=model,
        base_url=f"https://{host}/v1",
        api_key=f"key-{provider_id}",
        context_window=8_000,
        reasoning_effort="low",
    )


def _settings() -> Settings:
    return Settings(
        model_provider="primary",
        model_providers={
            "primary": _provider("primary", _PRIMARY_HOST, "primary-model"),
            "backup": _provider("backup", _BACKUP_HOST, "backup-model"),
        },
        purpose_effort={"default": "low"},
    )


def _request() -> LLMRequest:
    return LLMRequest(messages=[Message(role="user", content="ping")], max_tokens=16)


def _completion(model: str, text: str) -> dict:
    return {
        "model": model,
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }


def test_transport_falls_back_to_backup_provider() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == _PRIMARY_HOST:
            return httpx.Response(500, json={"error": "primary down"})
        return httpx.Response(200, json=_completion("backup-model", "backup ok"))

    llm = OpenAICompatLLM(
        settings=_settings(), transport=httpx.MockTransport(handler)
    )
    response = asyncio.run(llm.complete(_request()))

    assert calls == [_PRIMARY_HOST, _BACKUP_HOST]
    assert response.model == "backup-model"
    assert response.text == "backup ok"


def test_transport_all_providers_failed_raises_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    llm = OpenAICompatLLM(
        settings=_settings(), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(httpx.HTTPError):
        asyncio.run(llm.complete(_request()))
