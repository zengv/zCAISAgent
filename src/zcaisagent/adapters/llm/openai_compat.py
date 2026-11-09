import httpx

from zcaisagent.ports.llm import LLMDelta, LLMRequest, LLMResponse
from zcaisagent.settings import Effort, ModelProvider, Settings


class OpenAICompatLLM:
    """OpenAI-compatible chat completions adapter with multi-endpoint failover.

    降级策略由 Settings.fallback_chain(effort) 决定：
    同等级 provider 顺序 → 就近双向扩展到其他等级。
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
        # 调用方显式指定 reasoning_effort 时优先用；否则按 purpose 映射。
        effort: Effort
        if req.reasoning_effort is not None:
            effort = req.reasoning_effort
        else:
            effort = self.settings.resolve_effort(req.purpose)

        # 如果调用方显式指定了 model 名，尝试精确匹配 provider。
        if req.model:
            for p in self.settings.model_providers.values():
                if p.model == req.model:
                    return [p]

        return self.settings.fallback_chain(effort)

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
        cost = (
            tokens_in / 1e6 * self.input_price_per_mtok
            + tokens_out / 1e6 * self.output_price_per_mtok
        )
        return LLMResponse(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            reasoning_tokens=reasoning_tokens,
            cost_usd=round(cost, 6),
            model=data.get("model", payload["model"]),
        )

    async def stream(self, req: LLMRequest):
        response = await self.complete(req)
        yield LLMDelta(text=response.text)
