import json

import httpx

from zcaisagent.ports.llm import LLMDelta, LLMRequest, LLMResponse
from zcaisagent.settings import EmbeddingConfig, ModelProvider, Settings


class OpenAICompatLLM:
    """OpenAI-compatible chat completions adapter with multi-endpoint failover.

    未显式指定 reasoning_effort 时使用 config.toml 的默认 provider；
    显式指定时则按该等级构建降级链：同等级顺序 → 就近双向扩展到其他等级。
    每个 provider 用自己的 base_url / api_key / model 发起请求。
    """

    def __init__(
        self,
        settings: Settings,
        timeout_s: float = 60.0,
        input_price_per_mtok: float = 0.0,
        output_price_per_mtok: float = 0.0,
    ) -> None:
        self.settings = settings
        self.timeout_s = timeout_s
        self.input_price_per_mtok = input_price_per_mtok or settings.input_price_per_mtok
        self.output_price_per_mtok = (
            output_price_per_mtok or settings.output_price_per_mtok
        )

    def _resolve_chain(self, req: LLMRequest) -> list[ModelProvider]:
        # 如果调用方显式指定了 model 名，尝试精确匹配 provider。
        if req.model:
            for p in self.settings.model_providers.values():
                if p.model == req.model:
                    return [p]

        # 显式指定 reasoning_effort 时按该等级构建降级链。
        if req.reasoning_effort is not None:
            return self.settings.fallback_chain(req.reasoning_effort)

        # 未指定时直接使用默认 provider（config.toml 的 model_provider），
        # 再按它所属等级补齐备用链；没有默认 provider 才回退 purpose 映射。
        default = self.settings.default_provider()
        if default is None:
            return self.settings.fallback_chain(
                self.settings.resolve_effort(req.purpose)
            )
        chain = self.settings.fallback_chain(default.reasoning_effort)
        return [default] + [p for p in chain if p.id != default.id]

    async def complete(self, req: LLMRequest) -> LLMResponse:
        chain = self._resolve_chain(req)
        last_error: Exception | None = None
        for provider in chain:
            if not provider.api_key:
                continue
            try:
                return await self._complete_with_provider(provider, req)
            except httpx.HTTPError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise RuntimeError(
            "no usable provider — check config.toml and API key env vars"
        )

    async def _complete_with_provider(
        self, provider: ModelProvider, req: LLMRequest
    ) -> LLMResponse:
        payload = {
            "model": provider.model,
            "messages": [m.model_dump() for m in req.messages],
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
        }
        if req.disable_thinking:
            payload["chat_template_kwargs"] = {"thinking": False}
        headers = {"Authorization": f"Bearer {provider.api_key}"}
        timeout = req.timeout_s if req.timeout_s is not None else self.timeout_s
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{provider.base_url}/chat/completions", json=payload, headers=headers
            )
            response.raise_for_status()
            data = response.json()
        text = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens", 0))
        tokens_out = int(usage.get("completion_tokens", 0))
        details = usage.get("completion_tokens_details") or {}
        reasoning_tokens = int(details.get("reasoning_tokens", 0) or 0)
        prompt_details = usage.get("prompt_tokens_details") or {}
        cached_tokens = int(
            prompt_details.get("cached_tokens", 0)
            or usage.get("prompt_cache_hit_tokens", 0)
            or 0
        )
        cost = (
            tokens_in / 1e6 * self.input_price_per_mtok
            + tokens_out / 1e6 * self.output_price_per_mtok
        )
        return LLMResponse(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cached_tokens=cached_tokens,
            reasoning_tokens=reasoning_tokens,
            cost_usd=round(cost, 6),
            model=data.get("model", payload["model"]),
        )

    async def stream(self, req: LLMRequest):
        chain = self._resolve_chain(req)
        last_error: Exception | None = None
        for provider in chain:
            if not provider.api_key:
                continue
            # 首个 token 产出前可安全换 provider；已产出后再换会让前端文本重影。
            committed = False
            try:
                async for delta in self._stream_with_provider(provider, req):
                    if delta.text:
                        committed = True
                    yield delta
                return
            except httpx.HTTPError as exc:
                if committed:
                    raise
                last_error = exc
        if last_error is not None:
            raise last_error
        raise RuntimeError(
            "no usable provider — check config.toml and API key env vars"
        )

    async def _stream_with_provider(self, provider: ModelProvider, req: LLMRequest):
        payload = {
            "model": provider.model,
            "messages": [m.model_dump() for m in req.messages],
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if req.disable_thinking:
            payload["chat_template_kwargs"] = {"thinking": False}
        headers = {"Authorization": f"Bearer {provider.api_key}"}
        timeout = req.timeout_s if req.timeout_s is not None else self.timeout_s
        usage: dict = {}
        model_name = provider.model
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                f"{provider.base_url}/chat/completions",
                json=payload,
                headers=headers,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("model"):
                        model_name = chunk["model"]
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    piece = (choices[0].get("delta") or {}).get("content") or ""
                    if piece:
                        yield LLMDelta(text=piece)
        # usage 缺失时保持 0：不估算、不伪造成本，由调用方记录 stream_usage_missing。
        tokens_in = int(usage.get("prompt_tokens", 0))
        tokens_out = int(usage.get("completion_tokens", 0))
        details = usage.get("completion_tokens_details") or {}
        prompt_details = usage.get("prompt_tokens_details") or {}
        cost = (
            tokens_in / 1e6 * self.input_price_per_mtok
            + tokens_out / 1e6 * self.output_price_per_mtok
        )
        yield LLMDelta(
            text="",
            final=LLMResponse(
                text="",
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cached_tokens=int(prompt_details.get("cached_tokens", 0) or 0),
                reasoning_tokens=int(details.get("reasoning_tokens", 0) or 0),
                cost_usd=round(cost, 6),
                model=model_name,
            ),
        )


class OpenAICompatEmbedding:
    """OpenAI-compatible /embeddings adapter for the vector retrieval source."""

    def __init__(self, config: EmbeddingConfig, timeout_s: float = 30.0) -> None:
        self.config = config
        self.timeout_s = timeout_s

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            response = await client.post(
                f"{self.config.base_url}/embeddings",
                json={"model": self.config.model, "input": texts},
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()["data"]
        # 按 index 还原顺序，规避服务端乱序返回。
        ordered = sorted(data, key=lambda item: item["index"])
        return [item["embedding"] for item in ordered]
