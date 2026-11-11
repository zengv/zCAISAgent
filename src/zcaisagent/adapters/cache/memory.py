import copy
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from zcaisagent.ports.cache import CacheStats


class InMemoryCache:
    """进程内 TTL + LRU 缓存；值按不可变快照存取。

    ``ttl_s <= 0`` 表示永不过期（与记忆配置口径一致）；``max_entries <= 0``
    表示不限制容量。统计口径：命中 +1；未命中与过期均计为 miss。
    """

    def __init__(
        self,
        ttl_s: float = 300.0,
        max_entries: int = 1000,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._clock = clock or time.monotonic
        # key → (过期时间戳 | None, 值快照)；OrderedDict 头部为最久未用。
        self._entries: OrderedDict[str, tuple[float | None, Any]] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._lock = threading.Lock()

    async def get(self, key: str) -> Any | None:
        with self._lock:
            item = self._entries.get(key)
            if item is None:
                self._misses += 1
                return None
            expires_at, value = item
            if expires_at is not None and expires_at <= self._clock():
                self._entries.pop(key, None)
                self._misses += 1
                return None
            self._entries.move_to_end(key)
            self._hits += 1
            return copy.deepcopy(value)

    async def set(self, key: str, value: Any, ttl_s: float | None = None) -> None:
        ttl = self._ttl_s if ttl_s is None else ttl_s
        expires_at = None if ttl <= 0 else self._clock() + ttl
        with self._lock:
            self._entries[key] = (expires_at, copy.deepcopy(value))
            self._entries.move_to_end(key)
            if self._max_entries <= 0:
                return
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    async def delete_prefix(self, prefix: str) -> None:
        with self._lock:
            for key in [key for key in self._entries if key.startswith(prefix)]:
                self._entries.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._hits = 0
            self._misses = 0

    def stats(self) -> CacheStats:
        with self._lock:
            return CacheStats(hits=self._hits, misses=self._misses)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
