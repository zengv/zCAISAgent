import asyncio
from urllib.parse import parse_qs

import httpx
import pytest

from zcaisagent.adapters.security.oidc import OidcIdentity
from zcaisagent.domain.errors import ZCAISAgentError
from zcaisagent.settings import SecurityConfig

_SECURITY = SecurityConfig(
    roles={"support": ["retrieve", "tool:read"]},
    acl_tokens_by_role={"support": ["public", "support"]},
)

_CLAIMS = {
    "active": True,
    "sub": "alice",
    "tenant_id": "tenant-a",
    "roles": ["support"],
    "scope": "retrieve",
    "acl_tokens": ["public"],
}


def _identity(handler, security: SecurityConfig | None = _SECURITY) -> OidcIdentity:
    return OidcIdentity(
        "https://idp.example/introspect",
        "zcaisagent",
        "secret-value",
        transport=httpx.MockTransport(handler),
        security=security,
    )


def test_active_token_maps_claims_and_posts_credentials() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(
            {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        )
        return httpx.Response(200, json=_CLAIMS)

    auth = asyncio.run(_identity(handler).authenticate("token-1", {}))

    assert captured == {
        "token": "token-1",
        "client_id": "zcaisagent",
        "client_secret": "secret-value",
    }
    assert auth.tenant_id == "tenant-a"
    assert auth.user_id == "alice"
    assert auth.roles == ["support"]
    assert auth.scopes == ["retrieve", "tool:read"]
    assert auth.acl_tokens == ["public", "support"]


def test_inactive_token_is_unauthorized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"active": False})

    with pytest.raises(ZCAISAgentError) as exc:
        asyncio.run(_identity(handler).authenticate("token-1", {}))
    assert exc.value.code == "UNAUTHORIZED"
    assert exc.value.message == "invalid token"


def test_non_200_response_is_unauthorized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(ZCAISAgentError) as exc:
        asyncio.run(_identity(handler).authenticate("token-1", {}))
    assert exc.value.code == "UNAUTHORIZED"


def test_network_error_is_unauthorized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(ZCAISAgentError) as exc:
        asyncio.run(_identity(handler).authenticate("token-1", {}))
    assert exc.value.code == "UNAUTHORIZED"


def test_request_meta_cannot_override_claim_tenant() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_CLAIMS)

    auth = asyncio.run(
        _identity(handler).authenticate("token-1", {"tenant_id": "tenant-b"})
    )
    assert auth.tenant_id == "tenant-a"


def test_request_meta_tenant_used_only_when_claim_missing() -> None:
    claims = {key: value for key, value in _CLAIMS.items() if key != "tenant_id"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=claims)

    auth = asyncio.run(
        _identity(handler).authenticate("token-1", {"tenant_id": "tenant-b"})
    )
    assert auth.tenant_id == "tenant-b"
