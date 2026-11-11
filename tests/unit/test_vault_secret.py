import asyncio

import httpx

from zcaisagent.adapters.secret.vault import FallbackSecret, VaultSecret
from zcaisagent.governance.audit import EnvSecret


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _vault_response(data: dict) -> httpx.Response:
    return httpx.Response(200, json={"data": {"data": data}})


def test_reads_kv_v2_and_sends_token_header() -> None:
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return _vault_response({"api_key": "vault-key"})

    vault = VaultSecret(
        "http://vault.test/",
        "vault-token",
        mount="secret",
        path="zcaisagent",
        transport=httpx.MockTransport(handler),
    )

    assert asyncio.run(vault.get("api_key")) == "vault-key"
    request = captured["request"]
    assert str(request.url) == "http://vault.test/v1/secret/data/zcaisagent"
    assert request.headers["X-Vault-Token"] == "vault-token"
    assert vault.healthy is True
    assert vault.last_error == ""


def test_cached_value_skips_second_request() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return _vault_response({"api_key": "vault-key"})

    vault = VaultSecret(
        "http://vault.test",
        "token",
        transport=httpx.MockTransport(handler),
    )

    async def flow() -> None:
        assert await vault.get("api_key") == "vault-key"
        assert await vault.get("api_key") == "vault-key"

    asyncio.run(flow())
    assert calls["count"] == 1


def test_cache_expires_after_ttl() -> None:
    calls = {"count": 0}
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return _vault_response({"api_key": f"key-{calls['count']}"})

    vault = VaultSecret(
        "http://vault.test",
        "token",
        cache_ttl_s=10,
        clock=clock,
        transport=httpx.MockTransport(handler),
    )

    async def flow() -> None:
        assert await vault.get("api_key") == "key-1"
        clock.now = 11
        assert await vault.get("api_key") == "key-2"

    asyncio.run(flow())
    assert calls["count"] == 2


def test_not_found_falls_back_to_env_secret() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    vault = VaultSecret(
        "http://vault.test",
        "vault-token",
        fallback=EnvSecret({"api_key": "env-key"}),
        transport=httpx.MockTransport(handler),
    )

    assert asyncio.run(vault.get("api_key")) == "env-key"
    assert vault.healthy is False
    assert "404" in vault.last_error
    assert "vault-token" not in vault.last_error


def test_network_error_falls_back_and_hides_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("vault unavailable")

    vault = VaultSecret(
        "http://vault.test",
        "super-secret-token",
        fallback=EnvSecret({"api_key": "env-key"}),
        transport=httpx.MockTransport(handler),
    )

    assert asyncio.run(vault.get("api_key")) == "env-key"
    assert vault.healthy is False
    assert "ConnectError" in vault.last_error
    assert "super-secret-token" not in vault.last_error


def test_missing_key_falls_back() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _vault_response({"other": "value"})

    vault = VaultSecret(
        "http://vault.test",
        "token",
        fallback=EnvSecret({"api_key": "env-key"}),
        transport=httpx.MockTransport(handler),
    )

    assert asyncio.run(vault.get("api_key")) == "env-key"
    assert vault.healthy is False
    assert "key_not_found" in vault.last_error


def test_probe_reports_availability() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _vault_response({"api_key": "vault-key"})

    vault = VaultSecret(
        "http://vault.test",
        "token",
        transport=httpx.MockTransport(handler),
    )
    assert asyncio.run(vault.probe()) is True
    assert vault.healthy is True


class _BrokenPrimary:
    async def get(self, key: str) -> str:
        raise RuntimeError("primary down")


class _EmptyPrimary:
    async def get(self, key: str) -> str:
        return ""


def test_fallback_secret_uses_fallback_on_error() -> None:
    reports: list[tuple[bool, str]] = []

    secret = FallbackSecret(
        _BrokenPrimary(),
        EnvSecret({"api_key": "env-key"}),
        on_health=lambda ok, message: reports.append((ok, message)),
    )

    assert asyncio.run(secret.get("api_key")) == "env-key"
    assert secret.healthy is False
    assert reports == [(False, "RuntimeError")]


def test_fallback_secret_uses_fallback_on_empty_value() -> None:
    secret = FallbackSecret(_EmptyPrimary(), EnvSecret({"api_key": "env-key"}))

    assert asyncio.run(secret.get("api_key")) == "env-key"
    assert secret.healthy is False
    assert "primary_miss" in secret.last_error


def test_fallback_secret_prefers_primary_value() -> None:
    secret = FallbackSecret(
        EnvSecret({"api_key": "primary-key"}),
        EnvSecret({"api_key": "env-key"}),
    )

    assert asyncio.run(secret.get("api_key")) == "primary-key"
    assert secret.healthy is True
    assert secret.last_error == ""


def test_vault_does_not_use_fallback_without_instance() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    vault = VaultSecret(
        "http://vault.test", "token", transport=httpx.MockTransport(handler)
    )
    assert asyncio.run(vault.get("api_key")) == ""
    assert vault.healthy is False
