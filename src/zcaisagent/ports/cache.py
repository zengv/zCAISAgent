from typing import Any, Protocol

from pydantic import BaseModel


class CacheStats(BaseModel):
    hits: int = 0
    misses: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


class CachePort(Protocol):
    """结果缓存端口；value 必须是 dict/list/str 等 JSON 可序列化类型。"""

    async def get(self, key: str) -> Any | None: ...

    async def set(self, key: str, value: Any, ttl_s: float | None = None) -> None: ...

    async def delete_prefix(self, prefix: str) -> None: ...

    def stats(self) -> CacheStats: ...
