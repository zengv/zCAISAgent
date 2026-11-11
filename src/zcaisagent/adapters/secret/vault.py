"""Vault KV v2 密钥适配器：读取、TTL 缓存、失败回退与健康标记。

密钥只保存在进程内存中；错误信息会遮蔽 token 与已缓存的密钥值，
不会把明文写入日志或健康信息。
"""
from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from zcaisagent.ports.security import SecretPort


class VaultSecret:
    """从 Vault KV v2 读取密钥；失败时标记健康并回退 ``fallback``。"""

    def __init__(
        self,
        addr: str,
        token: str,
        mount: str = "secret",
        path: str = "zcaisagent",
        fallback: SecretPort | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 3.0,
        cache_ttl_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.addr = addr.rstrip("/")
        self.mount = mount.strip("/")
        self.path = path.strip("/")
        self._token = token
        self._fallback = fallback
        self._transport = transport
        self._timeout_s = timeout_s
        self._cache_ttl_s = cache_ttl_s
        self._clock = clock
        self._data: dict[str, str] = {}
        self._fetched_at: float | None = None
        self.healthy = False
        self.last_error = ""

    @property
    def url(self) -> str:
        return f"{self.addr}/v1/{self.mount}/data/{self.path}"

    async def get(self, key: str) -> str:
        data = await self._load()
        if data is not None:
            value = data.get(key)
            if value is not None:
                self.healthy = True
                self.last_error = ""
                return value
            self._fail(f"key_not_found: {key}")
        if self._fallback is not None:
            return await self._fallback.get(key)
        return ""

    async def probe(self) -> bool:
        """主动探测 Vault 可用性；不读取具体字段。"""
        return await self._load(force=True) is not None

    async def _load(self, force: bool = False) -> dict[str, str] | None:
        now = self._clock()
        if (
            not force
            and self._cache_ttl_s > 0
            and self._fetched_at is not None
            and now - self._fetched_at < self._cache_ttl_s
        ):
            return self._data
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout_s
            ) as client:
                response = await client.get(
                    self.url, headers={"X-Vault-Token": self._token}
                )
        except httpx.HTTPError as exc:
            self._fail(f"vault_request_failed: {type(exc).__name__}")
            return None
        if response.status_code != 200:
            self._fail(f"vault_status_{response.status_code}")
            return None
        try:
            raw = response.json()["data"]["data"]
            if not isinstance(raw, dict):
                raise TypeError("data.data is not a mapping")
        except (KeyError, TypeError, ValueError):
            self._fail("vault_invalid_payload")
            return None
        self._data = {str(item): str(value) for item, value in raw.items()}
        self._fetched_at = now
        self.healthy = True
        self.last_error = ""
        return self._data

    def _fail(self, message: str) -> None:
        self.healthy = False
        self.last_error = self._sanitize(message)

    def _sanitize(self, message: str) -> str:
        text = message
        if self._token:
            text = text.replace(self._token, "***")
        for value in self._data.values():
            if value:
                text = text.replace(value, "***")
        return text


class FallbackSecret:
    """主密钥源失败或返回空值时回退 ``fallback``，并上报健康状态。"""

    def __init__(
        self,
        primary: SecretPort,
        fallback: SecretPort,
        on_health: Callable[[bool, str], None] | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self._on_health = on_health
        self.healthy = False
        self.last_error = ""

    async def get(self, key: str) -> str:
        try:
            value = await self.primary.get(key)
        except Exception as exc:  # noqa: BLE001 - 主密钥源失败必须回退
            self._report(False, type(exc).__name__)
            return await self.fallback.get(key)
        if value:
            self._report(True, "")
            return value
        self._report(False, f"primary_miss: {key}")
        return await self.fallback.get(key)

    def _report(self, ok: bool, message: str) -> None:
        self.healthy = ok
        self.last_error = "" if ok else message
        if self._on_health is None:
            return
        try:
            self._on_health(ok, message)
        except Exception:  # noqa: BLE001 - 健康回调失败不影响取值
            return
