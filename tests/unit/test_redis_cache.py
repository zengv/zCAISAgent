import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from zcaisagent.adapters.cache.memory import InMemoryCache
from zcaisagent.adapters.cache.redis import CacheUnavailable, RedisCache
from zcaisagent.api import deps
from zcaisagent.api.app import app
from zcaisagent.container import build_cache, build_container
from zcaisagent.settings import CacheConfig, Settings


class FakeRedis:
    """最小 Redis 替身：dict 存储 + 记录 TTL 与删除批次。"""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}
        self.delete_batches: list[list[str]] = []
        self.closed = False

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.data[key] = value
        self.ttls[key] = ex
        return True

    async def delete(self, *keys: str) -> int:
        self.delete_batches.append(list(keys))
        removed = 0
        for key in keys:
            if self.data.pop(key, None) is not None:
                self.ttls.pop(key, None)
                removed += 1
        return removed

    async def scan_iter(
        self, match: str = "*", count: int | None = None
    ) -> AsyncIterator[str]:
        prefix = match.rstrip("*")
        for key in list(self.data):
            if key.startswith(prefix):
                yield key

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        self.closed = True


class FailingRedis:
    """所有命令都抛 ConnectionError，模拟不可达 Redis。"""

    def __init__(self) -> None:
        self.calls = 0

    def _fail(self) -> None:
        self.calls += 1
        raise ConnectionError("redis down")

    async def get(self, key: str) -> Any:
        self._fail()

    async def set(self, *args: Any, **kwargs: Any) -> Any:
        self._fail()

    async def delete(self, *keys: str) -> int:
        self._fail()
        return 0

    async def scan_iter(
        self, match: str = "*", count: int | None = None
    ) -> AsyncIterator[str]:
        self._fail()
        yield ""  # pragma: no cover - 仅为满足异步生成器语法

    async def ping(self) -> bool:
        self._fail()
        return False

    async def aclose(self) -> None:
        return None


def test_roundtrip_uses_prefix_json_and_ttl() -> None:
    client = FakeRedis()
    cache = RedisCache("redis://localhost:6379/0", prefix="zc:", ttl_s=30, client=client)

    async def flow() -> None:
        await cache.set("llm:a", {"v": [1, 2]})
        assert client.data["zc:llm:a"] == '{"v": [1, 2]}'
        assert client.ttls["zc:llm:a"] == 30
        assert await cache.get("llm:a") == {"v": [1, 2]}

        await cache.set("llm:a", {"v": 3}, ttl_s=5)
        assert client.ttls["zc:llm:a"] == 5

        await cache.set("llm:a", "plain", ttl_s=0)
        assert client.ttls["zc:llm:a"] is None

    asyncio.run(flow())
    stats = cache.stats()
    assert (stats.hits, stats.misses) == (1, 0)


def test_miss_and_corrupt_value_count_as_miss() -> None:
    client = FakeRedis()
    cache = RedisCache("redis://localhost:6379/0", prefix="zc:", client=client)

    async def flow() -> None:
        assert await cache.get("missing") is None
        client.data["zc:bad"] = "{not json"
        assert await cache.get("bad") is None
        assert "zc:bad" not in client.data

    asyncio.run(flow())
    stats = cache.stats()
    assert (stats.hits, stats.misses) == (0, 2)


def test_delete_prefix_scans_in_batches() -> None:
    client = FakeRedis()
    cache = RedisCache("redis://localhost:6379/0", prefix="zc:", client=client)

    async def flow() -> None:
        for index in range(120):
            await cache.set(f"retrieval:{index}", index)
        await cache.set("llm:a", 1)
        await cache.delete_prefix("retrieval:")
        assert set(client.data) == {"zc:llm:a"}
        assert len(client.delete_batches) == 2
        assert sum(len(batch) for batch in client.delete_batches) == 120

    asyncio.run(flow())


def test_redis_failure_degrades_to_fallback() -> None:
    client = FailingRedis()
    fallback = InMemoryCache()
    cache = RedisCache("redis://localhost:6379/0", client=client, fallback=fallback)

    async def flow() -> None:
        await cache.set("k", {"v": 1})
        assert cache.is_degraded is True
        # 降级后不再访问 Redis，直接命中内存回退。
        assert await cache.get("k") == {"v": 1}
        assert client.calls == 1

    asyncio.run(flow())
    assert cache.last_error.startswith("ConnectionError")


def test_redis_failure_without_fallback_raises() -> None:
    cache = RedisCache("redis://localhost:6379/0", client=FailingRedis())

    with pytest.raises(CacheUnavailable):
        asyncio.run(cache.get("k"))


def test_ping_and_close() -> None:
    client = FakeRedis()
    cache = RedisCache("redis://localhost:6379/0", client=client)

    assert asyncio.run(cache.ping()) is True
    asyncio.run(cache.close())
    assert client.closed is True

    failing = RedisCache("redis://localhost:6379/0", client=FailingRedis())
    assert asyncio.run(failing.ping()) is False
    assert failing.is_degraded is True


def test_build_cache_memory_backend() -> None:
    assert build_cache(Settings()) is None
    assert build_cache(Settings(cache=CacheConfig(enabled=False, backend="redis"))) is None
    assert isinstance(
        build_cache(Settings(cache=CacheConfig(enabled=True))), InMemoryCache
    )


def test_build_cache_redis_factory_failure_falls_back() -> None:
    settings = Settings(
        cache=CacheConfig(
            enabled=True, backend="redis", redis_url="redis://localhost:6379/0"
        )
    )

    def broken_factory(url: str, timeout: float) -> Any:
        raise ImportError("redis package is not installed")

    assert isinstance(build_cache(settings, client_factory=broken_factory), InMemoryCache)


def test_build_cache_redis_uses_injected_client() -> None:
    client = FakeRedis()
    settings = Settings(
        cache=CacheConfig(
            enabled=True, backend="redis", redis_url="redis://localhost:6379/0"
        )
    )

    cache = build_cache(settings, client_factory=lambda url, timeout: client)

    assert isinstance(cache, RedisCache)
    assert cache.is_degraded is False


def test_config_toml_cache_backend_defaults_to_memory(tmp_path: Path, monkeypatch) -> None:
    for key in list(os.environ):
        if key.startswith("ZCAISAGENT_"):
            monkeypatch.delenv(key)
    config_file = tmp_path / "config.toml"
    config_file.write_text("[cache]\nenabled = true\n", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")

    settings = Settings.from_env(env_file=env_file, config_file=config_file)

    assert settings.cache is not None
    assert settings.cache.backend == "memory"
    assert settings.cache.redis_url == ""
    assert settings.cache.redis_timeout_s == 1.0


def test_config_toml_and_env_override_redis_backend(tmp_path: Path, monkeypatch) -> None:
    for key in list(os.environ):
        if key.startswith("ZCAISAGENT_"):
            monkeypatch.delenv(key)
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[cache]
enabled = true
backend = "redis"
redis_url = "redis://cache:6379/1"
redis_timeout_s = 2.5
""",
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")

    settings = Settings.from_env(env_file=env_file, config_file=config_file)
    assert settings.cache is not None
    assert settings.cache.backend == "redis"
    assert settings.cache.redis_url == "redis://cache:6379/1"
    assert settings.cache.redis_timeout_s == 2.5

    monkeypatch.setenv("ZCAISAGENT_CACHE_BACKEND", "memory")
    monkeypatch.setenv("ZCAISAGENT_REDIS_URL", "redis://env:6379/2")
    monkeypatch.setenv("ZCAISAGENT_REDIS_TIMEOUT_S", "0.5")
    overridden = Settings.from_env(env_file=env_file, config_file=config_file)
    assert overridden.cache is not None
    assert overridden.cache.backend == "memory"
    assert overridden.cache.redis_url == "redis://env:6379/2"
    assert overridden.cache.redis_timeout_s == 0.5


def test_health_reports_cache_down_after_redis_failure(
    monkeypatch, tmp_path: Path
) -> None:
    container = build_container(
        Settings(data_dir=tmp_path, seed_dir=Path("data/seed")), ingest=False
    )
    cache = RedisCache(
        "redis://localhost:6379/0", client=FailingRedis(), fallback=InMemoryCache()
    )
    container.cache = cache

    async def warm() -> None:
        await cache.set("k", 1)

    asyncio.run(warm())
    assert cache.is_degraded is True

    monkeypatch.setattr(deps, "build_container", lambda: container)
    deps.get_container.cache_clear()
    try:
        response = TestClient(app).get("/health")
    finally:
        deps.get_container.cache_clear()

    body = response.json()
    entries = {entry["name"]: entry for entry in body["entries"]}
    assert body["status"] == "degraded"
    assert entries["cache"]["status"] == "down"
