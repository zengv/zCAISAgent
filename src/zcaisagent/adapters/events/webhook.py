"""Webhook 事件投递：HMAC-SHA256 签名、指数退避重试与死信 JSONL。

每个 URL 独立投递，单个订阅方失败不影响其他订阅方。2xx 视为成功；
5xx / 429 / 网络异常按 ``backoff_base_s * 2 ** (attempt - 1)`` 退避重试，
最多重试 ``max_retries`` 次（总尝试 ``max_retries + 1``）；其余 4xx 不重试。
最终失败写入死信，死信写失败只忽略，不向上抛异常。签名密钥不写入死信。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from zcaisagent.ports.events import EventEnvelope

Sleep = Callable[[float], Awaitable[None]]


class WebhookEventSink:
    """向一个或多个 Webhook 地址投递事件。"""

    def __init__(
        self,
        urls: list[str],
        secret: str,
        max_retries: int = 3,
        dead_letter_path: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 3.0,
        backoff_base_s: float = 0.05,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.urls = [url for url in urls if url]
        self.secret = secret
        self.max_retries = max(0, int(max_retries))
        self.dead_letter_path = dead_letter_path
        self.transport = transport
        self.timeout_s = timeout_s
        self.backoff_base_s = backoff_base_s
        self._sleep = sleep or asyncio.sleep

    def signature(self, body: bytes) -> str:
        digest = hmac.new(
            self.secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        return f"sha256={digest}"

    async def publish(self, event: EventEnvelope) -> None:
        if not self.urls:
            return
        body = json.dumps(
            event.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-ZCAIS-Signature": self.signature(body),
            "X-ZCAIS-Event-Id": event.id,
            "X-ZCAIS-Event-Type": event.type,
        }
        for url in self.urls:
            try:
                await self._deliver(url, body, headers, event)
            except Exception:  # noqa: BLE001 - 单个订阅方失败不影响其他 URL
                continue

    async def _deliver(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        event: EventEnvelope,
    ) -> None:
        attempts = 0
        status: int | None = None
        error = ""
        while attempts <= self.max_retries:
            attempts += 1
            try:
                async with httpx.AsyncClient(
                    transport=self.transport, timeout=self.timeout_s
                ) as client:
                    response = await client.post(url, content=body, headers=headers)
            except httpx.HTTPError as exc:
                status = None
                error = f"{type(exc).__name__}: {exc}"
            else:
                status = response.status_code
                if 200 <= status < 300:
                    return
                error = f"http_{status}"
                if status != 429 and not 500 <= status < 600:
                    break
            if attempts <= self.max_retries:
                await self._sleep(self.backoff_base_s * (2 ** (attempts - 1)))
        self._dead_letter(event, url, attempts, status, error)

    def _dead_letter(
        self,
        event: EventEnvelope,
        url: str,
        attempts: int,
        status: int | None,
        error: str,
    ) -> None:
        if self.dead_letter_path is None:
            return
        record = {
            "event_id": event.id,
            "type": event.type,
            "url": url,
            "attempts": attempts,
            "status": status,
            "error": self._mask(error),
            "ts": time.time(),
        }
        try:
            self.dead_letter_path.parent.mkdir(parents=True, exist_ok=True)
            with self.dead_letter_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            return

    def _mask(self, text: str) -> str:
        if self.secret:
            return text.replace(self.secret, "***")
        return text
