import asyncio

from zcaisagent.adapters.cache.memory import InMemoryCache


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_ttl_expiry_counts_as_miss() -> None:
    clock = FakeClock()
    cache = InMemoryCache(ttl_s=10, clock=clock)

    async def flow() -> None:
        await cache.set("k", {"v": 1})
        assert await cache.get("k") == {"v": 1}
        clock.now = 10.0
        assert await cache.get("k") is None

    asyncio.run(flow())
    stats = cache.stats()
    assert (stats.hits, stats.misses) == (1, 1)
    assert stats.hit_rate == 0.5


def test_set_ttl_overrides_default() -> None:
    clock = FakeClock()
    cache = InMemoryCache(ttl_s=100, clock=clock)

    async def flow() -> None:
        await cache.set("short", 1, ttl_s=5)
        clock.now = 6
        assert await cache.get("short") is None

    asyncio.run(flow())


def test_lru_evicts_least_recently_used() -> None:
    cache = InMemoryCache(ttl_s=0, max_entries=2)

    async def flow() -> None:
        await cache.set("a", 1)
        await cache.set("b", 2)
        assert await cache.get("a") == 1  # 访问 a，b 变为最久未用
        await cache.set("c", 3)
        assert await cache.get("b") is None
        assert await cache.get("a") == 1
        assert await cache.get("c") == 3

    asyncio.run(flow())


def test_delete_prefix_only_removes_matching_keys() -> None:
    cache = InMemoryCache()

    async def flow() -> None:
        await cache.set("retrieval:a", 1)
        await cache.set("retrieval:b", 2)
        await cache.set("llm:a", 3)
        await cache.delete_prefix("retrieval:")
        assert await cache.get("retrieval:a") is None
        assert await cache.get("retrieval:b") is None
        assert await cache.get("llm:a") == 3

    asyncio.run(flow())


def test_values_are_snapshotted() -> None:
    cache = InMemoryCache()
    payload = {"items": [1, 2]}

    async def flow() -> None:
        await cache.set("k", payload)
        payload["items"].append(3)
        first = await cache.get("k")
        assert first == {"items": [1, 2]}
        first["items"].append(4)
        assert await cache.get("k") == {"items": [1, 2]}

    asyncio.run(flow())


def test_stats_and_clear() -> None:
    cache = InMemoryCache()

    async def flow() -> None:
        await cache.set("k", 1)
        await cache.get("k")
        await cache.get("missing")

    asyncio.run(flow())
    stats = cache.stats()
    assert (stats.hits, stats.misses) == (1, 1)
    cache.clear()
    assert cache.stats().hits == 0
    assert cache.stats().misses == 0
    assert len(cache) == 0
