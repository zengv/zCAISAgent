import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any

from zcaisagent.ports.cache import CachePort
from zcaisagent.ports.llm import LLMDelta, LLMPort, LLMRequest, LLMResponse


class CachedLLM:
    """LLM 结果缓存装饰器：仅缓存 temperature=0 且非流式的确定性请求。"""

    def __init__(self, inner: LLMPort, cache: CachePort, ttl_s: float = 300.0) -> None:
        self._inner = inner
        self._cache = cache
        self._ttl_s = ttl_s

    async def complete(self, req: LLMRequest) -> LLMResponse:
        if req.temperature != 0:
            return await self._inner.complete(req)

        key = self._key(req)
        try:
            cached: Any | None = await self._cache.get(key)
        except Exception:  # noqa: BLE001 - 缓存故障不阻断模型调用
            cached = None
        if cached is not None:
            try:
                hit = LLMResponse.model_validate(cached)
            except Exception:  # noqa: BLE001 - 缓存内容损坏时回落真实调用
                hit = None
            if hit is not None:
                return hit.model_copy(
                    update={
                        "tokens_in": 0,
                        "tokens_out": 0,
                        "cost_usd": 0.0,
                        "cached_tokens": hit.tokens_in,
                        "cache_hit": True,
                    }
                )

        response = await self._inner.complete(req)
        try:
            await self._cache.set(key, response.model_dump(mode="json"), self._ttl_s)
        except Exception:  # noqa: BLE001 - 缓存写入失败不影响本次结果
            pass
        return response

    def stream(self, req: LLMRequest) -> AsyncIterator[LLMDelta]:
        return self._inner.stream(req)

    def _key(self, req: LLMRequest) -> str:
        model = req.model or self._inner_model()
        messages = json.dumps(
            [message.model_dump(mode="json") for message in req.messages],
            ensure_ascii=False,
            sort_keys=True,
        )
        digest = hashlib.sha256(messages.encode("utf-8")).hexdigest()
        return f"llm:{model}:{req.purpose}:{req.max_tokens}:{digest}"

    def _inner_model(self) -> str:
        settings = getattr(self._inner, "settings", None)
        if settings is not None:
            return str(getattr(settings, "default_model_name", "") or "")
        return str(getattr(self._inner, "model", "") or "")
