import asyncio

import httpx
import pytest

from zcaisagent.adapters.llm.openai_compat import OpenAICompatLLM
from zcaisagent.domain.models import Message
from zcaisagent.ports.llm import LLMRequest
from zcaisagent.settings import ModelProvider, Settings


def _mk_settings(
    providers: list[tuple[str, str, str]],
    *,
    input_price: float = 0.0,
    output_price: float = 0.0,
    purpose_effort: dict[str, str] | None = None,
) -> Settings:
    """为测试构造一个最小 Settings。

    providers: [(provider_id, model_name, api_key), ...]
    """
    provs = {}
    for pid, model, key in providers:
        provs[pid] = ModelProvider(
            id=pid,
            name=pid,
            model=model,
            base_url="http://example/v1",
            api_key=key,
            context_window=128000,
            reasoning_effort="low",
        )
    first_id = next(iter(provs)) if provs else ""
    return Settings(
        model_provider=first_id,
        model_providers=provs,
        purpose_effort={"default": "low", **(purpose_effort or {})},
        input_price_per_mtok=input_price,
        output_price_per_mtok=output_price,
    )


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "model": "deepseek-flash",
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
        }


class _FakeClient:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        return _FakeResponse()


def _run_llm(monkeypatch, settings: Settings) -> object:
    monkeypatch.setattr(
        "zcaisagent.adapters.llm.openai_compat.httpx.AsyncClient", _FakeClient
    )
    llm = OpenAICompatLLM(settings=settings)
    request = LLMRequest(messages=[Message(role="user", content="hi")])
    return asyncio.run(llm.complete(request))


def test_cost_is_computed_from_configured_prices(monkeypatch) -> None:
    settings = _mk_settings(
        [("ds", "deepseek-flash", "k")], input_price=2.0, output_price=6.0
    )
    response = _run_llm(monkeypatch, settings)
    assert response.tokens_in == 1000
    assert response.tokens_out == 500
    assert response.cost_usd == pytest.approx(0.005)


def test_provider_price_overrides_global_price(monkeypatch) -> None:
    provs = {
        "ds": ModelProvider(
            id="ds", name="ds", model="deepseek-flash",
            base_url="http://example/v1", api_key="k", context_window=128000,
            reasoning_effort="low", cost_in=1.0, cost_out=3.0,
        ),
    }
    settings = Settings(
        model_provider="ds",
        model_providers=provs,
        purpose_effort={"default": "low"},
        input_price_per_mtok=2.0,
        output_price_per_mtok=6.0,
    )
    response = _run_llm(monkeypatch, settings)
    # 1000 in × $1/M + 500 out × $3/M = 0.0025（provider 单价优先于全局单价）
    assert response.cost_usd == pytest.approx(0.0025)


def test_cost_stays_zero_when_prices_not_configured(monkeypatch) -> None:
    settings = _mk_settings([("ds", "deepseek-flash", "k")])
    response = _run_llm(monkeypatch, settings)
    assert response.cost_usd == 0.0


class _FailThenSucceedClient:
    calls: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, json: dict, **kwargs):
        self.calls.append(json["model"])
        if len(self.calls) == 1:
            raise httpx.ConnectError("primary model unavailable")
        return _FakeResponse()


def test_falls_back_to_next_provider_in_chain(monkeypatch) -> None:
    monkeypatch.setattr(
        "zcaisagent.adapters.llm.openai_compat.httpx.AsyncClient", _FailThenSucceedClient
    )
    _FailThenSucceedClient.calls = []

    # 两个 low provider：primary 挂了后自动降到 backup
    provs = {
        "primary": ModelProvider(
            id="primary", name="primary", model="primary",
            base_url="http://example/v1", api_key="k", context_window=128000,
            reasoning_effort="low",
        ),
        "backup": ModelProvider(
            id="backup", name="backup", model="backup",
            base_url="http://example/v1", api_key="k", context_window=128000,
            reasoning_effort="low",
        ),
    }
    settings = Settings(
        model_provider="primary",
        model_providers=provs,
        purpose_effort={"default": "low"},
    )
    llm = OpenAICompatLLM(settings=settings)
    request = LLMRequest(messages=[Message(role="user", content="hi")])
    response = asyncio.run(llm.complete(request))
    assert _FailThenSucceedClient.calls == ["primary", "backup"]
    assert response.text == "ok"


class _RecordingClient:
    calls: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, json: dict, **kwargs):
        self.calls.append(json["model"])
        return _FakeResponse()


def test_default_provider_wins_over_purpose_mapping(monkeypatch) -> None:
    monkeypatch.setattr(
        "zcaisagent.adapters.llm.openai_compat.httpx.AsyncClient", _RecordingClient
    )
    _RecordingClient.calls = []

    # model_provider 是 high 等级，purpose 映射却指向 low：未显式指定 effort
    # 时应直接使用默认 provider，而不是按 purpose 选 low。
    provs = {
        "low": ModelProvider(
            id="low", name="low", model="low-model",
            base_url="http://example/v1", api_key="k", context_window=128000,
            reasoning_effort="low",
        ),
        "high": ModelProvider(
            id="high", name="high", model="high-model",
            base_url="http://example/v1", api_key="k", context_window=128000,
            reasoning_effort="high",
        ),
    }
    settings = Settings(
        model_provider="high",
        model_providers=provs,
        purpose_effort={"default": "low"},
    )
    llm = OpenAICompatLLM(settings=settings)
    request = LLMRequest(messages=[Message(role="user", content="hi")])
    asyncio.run(llm.complete(request))
    assert _RecordingClient.calls == ["high-model"]


def test_explicit_effort_overrides_default_provider(monkeypatch) -> None:
    monkeypatch.setattr(
        "zcaisagent.adapters.llm.openai_compat.httpx.AsyncClient", _RecordingClient
    )
    _RecordingClient.calls = []

    provs = {
        "low": ModelProvider(
            id="low", name="low", model="low-model",
            base_url="http://example/v1", api_key="k", context_window=128000,
            reasoning_effort="low",
        ),
        "high": ModelProvider(
            id="high", name="high", model="high-model",
            base_url="http://example/v1", api_key="k", context_window=128000,
            reasoning_effort="high",
        ),
    }
    settings = Settings(
        model_provider="high",
        model_providers=provs,
        purpose_effort={"default": "low"},
    )
    llm = OpenAICompatLLM(settings=settings)
    request = LLMRequest(
        messages=[Message(role="user", content="hi")], reasoning_effort="low"
    )
    asyncio.run(llm.complete(request))
    assert _RecordingClient.calls == ["low-model"]
