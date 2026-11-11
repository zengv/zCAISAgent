"""Redis 结果缓存适配器：JSON 序列化、TTL、前缀删除与降级回退。

``redis.asyncio`` 是可选依赖：未安装、连接失败或命令异常统一抛
``CacheUnavailable``；构造时注入的 ``fallback`` 会承接后续读写，并把
``is_degraded`` 置为 True，健康板据此报 DOWN。
"""
from __future__ import annotations

import json
from typing import Any

from zcaisagent.ports.cache import CachePort, CacheStats


class CacheUnavailable(Exception):
    """Redis 缓存不可用（依赖缺失 / 连接失败 / 命令异常）。"""


class RedisCache:
    """Redis 缓存；失败时降级到 ``fallback``（通常是 InMemoryCache）。

    ``client`` 可注入测试替身，鸭子类型只需实现 async
    ``get/set/delete/scan_iter/ping/aclose``。
    """

    def __init__(
        self,
        url: str,
        *,
        prefix: str = "zcaisagent:",
        ttl_s: float = 300.0,
        timeout_s: float = 1.0,
        client: Any | None = None,
        fallback: CachePort | None = None,
    ) -> None:
        self._url = url
        self._prefix = prefix
        self._ttl_s = ttl_s
        self._timeout_s = timeout_s
        self._injected = client
        self._client_instance: Any | None = None
        self._fallback = fallback
        self._hits = 0
        self._misses = 0
        self.degraded = False
        self.last_error = ""

    @property
    def is_degraded(self) -> bool:
        return self.degraded

    async def get(self, key: str) -> Any | None:
        if self.degraded:
            return await self._fallback_get(key)
        full = self._key(key)
        try:
            raw = await self._client().get(full)
        except Exception as exc:  # noqa: BLE001 - Redis 故障统一降级
            return await self._fallback_get(key, exc)
        if raw is None:
            self._misses += 1
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            # 坏值按 miss 处理并删除，下次写入会覆盖。
            self._misses += 1
            await self._delete_raw(full)
            return None
        self._hits += 1
        return value

    async def set(self, key: str, value: Any, ttl_s: float | None = None) -> None:
        if self.degraded:
            await self._fallback_set(key, value, ttl_s)
            return
        ttl = self._ttl_s if ttl_s is None else ttl_s
        payload = json.dumps(value, ensure_ascii=False, default=str)
        full = self._key(key)
        try:
            client = self._client()
            if ttl > 0:
                # SETEX 语义：redis-py 以 set(ex=...) 发送带秒级过期的 SET。
                await client.set(full, payload, ex=max(1, int(round(ttl))))
            else:
                await client.set(full, payload)
        except Exception as exc:  # noqa: BLE001 - Redis 故障统一降级
            await self._fallback_set(key, value, ttl_s, exc)

    async def delete_prefix(self, prefix: str) -> None:
        if self.degraded:
            await self._fallback_delete(prefix)
            return
        batch: list[str] = []
        try:
            client = self._client()
            async for key in client.scan_iter(match=f"{self._prefix}{prefix}*", count=100):
                batch.append(key)
                if len(batch) >= 100:
                    await client.delete(*batch)
                    batch.clear()
            if batch:
                await client.delete(*batch)
        except Exception as exc:  # noqa: BLE001 - Redis 故障统一降级
            await self._fallback_delete(prefix, exc)

    def stats(self) -> CacheStats:
        return CacheStats(hits=self._hits, misses=self._misses)

    async def ping(self) -> bool:
        """探测连通性；成功会清除降级标记，便于恢复后重新启用 Redis。"""
        try:
            ok = bool(await self._client().ping())
        except Exception as exc:  # noqa: BLE001 - 探测失败即降级
            self._mark_degraded(exc)
            return False
        if ok:
            self.degraded = False
            self.last_error = ""
        return ok

    async def close(self) -> None:
        client = self._injected if self._injected is not None else self._client_instance
        self._client_instance = None
        if client is None:
            return
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 - 关闭失败不影响进程退出
            pass

    def _client(self) -> Any:
        """懒连接：首次使用时导入 redis.asyncio 并建立客户端。"""
        if self._injected is not None:
            return self._injected
        if self._client_instance is None:
            try:
                from redis import asyncio as redis_asyncio
            except ImportError as exc:
                raise CacheUnavailable("redis package is not installed") from exc
            try:
                self._client_instance = redis_asyncio.from_url(
                    self._url,
                    socket_timeout=self._timeout_s,
                    socket_connect_timeout=self._timeout_s,
                    decode_responses=True,
                )
            except Exception as exc:  # noqa: BLE001 - 连接参数错误同样降级
                raise CacheUnavailable(f"redis client init failed: {exc}") from exc
        return self._client_instance

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    async def _delete_raw(self, full_key: str) -> None:
        try:
            await self._client().delete(full_key)
        except Exception:  # noqa: BLE001 - 坏值删除失败不影响本 miss
            pass

    def _mark_degraded(self, exc: Exception) -> None:
        self.degraded = True
        message = f"{type(exc).__name__}: {exc}"
        if self._url:
            message = message.replace(self._url, "redis://***")
        self.last_error = message

    async def _fallback_get(self, key: str, exc: Exception | None = None) -> Any | None:
        if exc is not None:
            self._mark_degraded(exc)
        if self._fallback is None:
            raise CacheUnavailable(self.last_error or "redis unavailable")
        return await self._fallback.get(key)

    async def _fallback_set(
        self,
        key: str,
        value: Any,
        ttl_s: float | None,
        exc: Exception | None = None,
    ) -> None:
        if exc is not None:
            self._mark_degraded(exc)
        if self._fallback is None:
            raise CacheUnavailable(self.last_error or "redis unavailable")
        await self._fallback.set(key, value, ttl_s)

    async def _fallback_delete(
        self, prefix: str, exc: Exception | None = None
    ) -> None:
        if exc is not None:
            self._mark_degraded(exc)
        if self._fallback is None:
            raise CacheUnavailable(self.last_error or "redis unavailable")
        await self._fallback.delete_prefix(prefix)
