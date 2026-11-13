"""zCAISAgent Python SDK：基于 httpx 的轻量异步客户端。

只依赖 ``httpx`` 与标准库，不导入服务端的 FastAPI / 适配器模块，可直接用于
脚本、Notebook 或测试。所有请求携带 ``X-API-Key``；非 2xx 响应按状态码映射
到 :class:`ZCAISAPIError` 子类。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx


class ZCAISError(Exception):
    """SDK 异常基类。"""


class ZCAISAPIError(ZCAISError):
    """服务端返回非 2xx 时的通用错误。"""

    def __init__(
        self,
        status_code: int,
        message: str = "",
        *,
        code: str = "",
        body: Any = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.body = body
        text = " ".join(str(part) for part in (status_code, code, message) if part)
        super().__init__(text)


class ZCAISAuthError(ZCAISAPIError):
    """401：API Key 缺失或无效。"""


class ZCAISForbiddenError(ZCAISAPIError):
    """403：无权访问该资源。"""


class ZCAISNotFoundError(ZCAISAPIError):
    """404：Run 或资源不存在。"""


class ZCAISConflictError(ZCAISAPIError):
    """409：状态冲突（如 Run 已取消或审批已决策）。"""


def _run_payload(
    goal: str,
    *,
    thread_id: str | None = None,
    tenant_id: str | None = None,
    user_id: str = "dev",
    level: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"goal": goal, "user_id": user_id}
    if thread_id is not None:
        payload["thread_id"] = thread_id
    if tenant_id is not None:
        payload["tenant_id"] = tenant_id
    if level is not None:
        payload["level"] = level
    return payload


def _optional_params(**values: str | None) -> dict[str, str]:
    return {key: value for key, value in values.items() if value is not None}


def _error_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _error_details(body: Any) -> tuple[str, str]:
    """提取 FastAPI ``{"detail": ...}`` 或 SDK ``{"code", "message"}`` 信息。"""
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str):
            return str(body.get("code") or ""), detail
        if isinstance(detail, dict):
            body = {**body, **detail}
        return str(body.get("code") or ""), str(body.get("message") or "")
    return "", str(body or "")


def _dispatch_sse(event: str, data: list[str]) -> tuple[str, dict] | None:
    """把一段 SSE 事件转成 ``(event, data)``；忽略空数据与非 JSON 数据。"""
    if not data:
        return None
    raw = "\n".join(data)
    if raw.strip() == "[DONE]":
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return event, payload


async def _iter_sse(response: httpx.Response) -> AsyncIterator[tuple[str, dict]]:
    event = "message"
    data: list[str] = []
    async for line in response.aiter_lines():
        if not line:
            item = _dispatch_sse(event, data)
            event, data = "message", []
            if item is not None:
                yield item
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
    item = _dispatch_sse(event, data)
    if item is not None:
        yield item


class ZCAISClient:
    """zCAISAgent 异步客户端；``base_url`` 如 ``http://127.0.0.1:8000``。"""

    def __init__(
        self,
        base_url: str,
        api_key: str = "dev-key",
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_s = timeout_s
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"X-API-Key": self.api_key},
                timeout=self.timeout_s,
                transport=self._transport,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> ZCAISClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        body = _error_body(response)
        code, message = _error_details(body)
        error_type: type[ZCAISAPIError] = {
            401: ZCAISAuthError,
            403: ZCAISForbiddenError,
            404: ZCAISNotFoundError,
            409: ZCAISConflictError,
        }.get(response.status_code, ZCAISAPIError)
        raise error_type(response.status_code, message, code=code, body=body)

    async def create_run(
        self,
        goal: str,
        *,
        thread_id: str | None = None,
        tenant_id: str | None = None,
        user_id: str = "dev",
        level: str | None = None,
    ) -> dict:
        response = await self._http.post(
            f"{self.base_url}/v1/runs",
            json=_run_payload(
                goal,
                thread_id=thread_id,
                tenant_id=tenant_id,
                user_id=user_id,
                level=level,
            ),
        )
        self._raise_for_status(response)
        return response.json()

    async def get_run(
        self,
        run_id: str,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> dict:
        response = await self._http.get(
            f"{self.base_url}/v1/runs/{run_id}",
            params=_optional_params(tenant_id=tenant_id, user_id=user_id),
        )
        self._raise_for_status(response)
        return response.json()

    async def trace(
        self,
        run_id: str,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> dict:
        response = await self._http.get(
            f"{self.base_url}/v1/runs/{run_id}/trace",
            params=_optional_params(tenant_id=tenant_id, user_id=user_id),
        )
        self._raise_for_status(response)
        return response.json()

    async def _decide(
        self,
        action: str,
        run_id: str,
        reason: str | None,
        actor: str | None,
    ) -> dict:
        payload = _optional_params(reason=reason, actor=actor)
        response = await self._http.post(
            f"{self.base_url}/v1/runs/{run_id}/{action}",
            json=payload or None,
        )
        self._raise_for_status(response)
        return response.json()

    async def approve(
        self,
        run_id: str,
        *,
        reason: str | None = None,
        actor: str | None = None,
    ) -> dict:
        return await self._decide("approve", run_id, reason, actor)

    async def reject(
        self,
        run_id: str,
        *,
        reason: str | None = None,
        actor: str | None = None,
    ) -> dict:
        return await self._decide("reject", run_id, reason, actor)

    async def cancel(self, run_id: str) -> dict:
        response = await self._http.post(f"{self.base_url}/v1/runs/{run_id}/cancel")
        self._raise_for_status(response)
        return response.json()

    async def metrics(self, window: str = "24h") -> dict:
        response = await self._http.get(
            f"{self.base_url}/v1/admin/metrics", params={"window": window}
        )
        self._raise_for_status(response)
        return response.json()

    async def health(self) -> dict:
        response = await self._http.get(f"{self.base_url}/health")
        self._raise_for_status(response)
        return response.json()

    async def ask_n2sql(
        self,
        question: str,
        *,
        user_id: str = "dev",
        tenant_id: str | None = None,
    ) -> dict:
        """POST /v1/n2sql，返回答案、SQL、结果列与结果行。"""
        payload: dict[str, Any] = {"question": question, "user_id": user_id}
        if tenant_id is not None:
            payload["tenant_id"] = tenant_id
        response = await self._http.post(f"{self.base_url}/v1/n2sql", json=payload)
        self._raise_for_status(response)
        return response.json()

    async def chat_stream(
        self,
        goal: str,
        *,
        thread_id: str | None = None,
        tenant_id: str | None = None,
        user_id: str = "dev",
        level: str | None = None,
    ) -> AsyncIterator[tuple[str, dict]]:
        """POST /v1/chat 并按 SSE 事件逐个 yield ``(event, data)``。"""
        payload = _run_payload(
            goal,
            thread_id=thread_id,
            tenant_id=tenant_id,
            user_id=user_id,
            level=level,
        )
        async with self._http.stream(
            "POST", f"{self.base_url}/v1/chat", json=payload
        ) as response:
            if not response.is_success:
                await response.aread()
                self._raise_for_status(response)
            async for item in _iter_sse(response):
                yield item
