import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zcaisagent.adapters.cache.llm import CachedLLM
from zcaisagent.api.app import app
from zcaisagent.api.routes import admin
from zcaisagent.container import build_container
from zcaisagent.settings import CacheConfig, Settings


def _container(tmp_path: Path, cache: CacheConfig | None = None):
    return build_container(
        Settings(data_dir=tmp_path, seed_dir=Path("data/seed"), cache=cache)
    )


def test_default_container_reports_disabled(monkeypatch, tmp_path: Path) -> None:
    container = _container(tmp_path)
    monkeypatch.setattr(admin, "get_container", lambda: container)

    response = TestClient(app).get("/v1/admin/cache/stats")

    assert response.status_code == 200
    assert response.json() == {
        "enabled": False,
        "hits": 0,
        "misses": 0,
        "hit_rate": 0.0,
    }


def test_cache_stats_reports_hits_and_rate(monkeypatch, tmp_path: Path) -> None:
    container = _container(
        tmp_path,
        CacheConfig(enabled=True, ttl_s=60, max_entries=10, llm_enabled=True),
    )
    assert container.cache is not None
    assert isinstance(container.llm, CachedLLM)

    async def warm() -> None:
        await container.cache.set("k", {"v": 1})
        await container.cache.get("k")
        await container.cache.get("missing")
        await container.cache.get("missing")

    asyncio.run(warm())
    monkeypatch.setattr(admin, "get_container", lambda: container)

    body = TestClient(app).get("/v1/admin/cache/stats").json()

    assert body["enabled"] is True
    assert body["hits"] == 1
    assert body["misses"] == 2
    assert body["hit_rate"] == pytest.approx(1 / 3)
