import asyncio

import pytest

from zcaisagent.domain.errors import ZCAISAgentError
from zcaisagent.governance.identity import (
    ApiKeyIdentity,
    RbacPolicy,
    acl_tokens_for_roles,
    build_auth_context,
    scopes_for_roles,
)
from zcaisagent.ports.security import AuthContext, Resource
from zcaisagent.settings import SecurityConfig

_SECURITY = SecurityConfig(
    roles={
        "support": ["retrieve", "tool:read"],
        "admin": ["retrieve", "tool:read", "tool:write", "run:create", "run:approve"],
    },
    acl_tokens_by_role={"support": ["public"], "admin": ["public", "finance"]},
)


def _ctx(scopes: list[str], tenant: str = "demo") -> AuthContext:
    return AuthContext(
        tenant_id=tenant,
        user_id="alice",
        roles=["support"],
        scopes=scopes,
        acl_tokens=["public"],
    )


def test_role_maps_to_configured_scopes_and_acl() -> None:
    assert scopes_for_roles(["support"], _SECURITY) == ["retrieve", "tool:read"]
    assert acl_tokens_for_roles(["support"], _SECURITY) == ["public"]
    assert scopes_for_roles(["admin"], _SECURITY) == [
        "retrieve",
        "tool:read",
        "tool:write",
        "run:create",
        "run:approve",
    ]
    assert acl_tokens_for_roles(["admin"], _SECURITY) == ["public", "finance"]


def test_undeclared_role_has_no_scope() -> None:
    assert scopes_for_roles(["unknown"], _SECURITY) == []
    assert acl_tokens_for_roles(["unknown"], _SECURITY) == []


def test_default_admin_scopes_without_security_config() -> None:
    assert scopes_for_roles(["admin"], None) == [
        "retrieve",
        "tool:read",
        "run:create",
        "admin:read",
        "admin:write",
    ]
    assert acl_tokens_for_roles(["admin"], None) == ["public"]
    assert scopes_for_roles(["support"], None) == []


def test_api_key_identity_keeps_two_arg_constructor() -> None:
    identity = ApiKeyIdentity("dev-key", "demo")
    auth = asyncio.run(identity.authenticate("dev-key", {}))

    assert auth.tenant_id == "demo"
    assert auth.roles == ["admin"]
    assert auth.scopes == [
        "retrieve",
        "tool:read",
        "run:create",
        "admin:read",
        "admin:write",
    ]
    assert auth.acl_tokens == ["public"]


def test_api_key_identity_uses_configured_role_mapping() -> None:
    identity = ApiKeyIdentity("dev-key", "demo", _SECURITY, roles=["support"])
    auth = asyncio.run(identity.authenticate("dev-key", {"user_id": "alice"}))

    assert auth.scopes == ["retrieve", "tool:read"]
    assert auth.acl_tokens == ["public"]


def test_api_key_identity_rejects_invalid_key() -> None:
    with pytest.raises(ZCAISAgentError) as exc:
        asyncio.run(ApiKeyIdentity("dev-key", "demo").authenticate("wrong", {}))
    assert exc.value.code == "UNAUTHORIZED"


def test_rbac_requires_scope_and_matching_tenant() -> None:
    policy = RbacPolicy(_SECURITY)

    allowed = asyncio.run(
        policy.authorize("tool:read", Resource(kind="tool", tenant_id="demo"), _ctx(["tool:read"]))
    )
    assert allowed.allowed is True

    missing = asyncio.run(
        policy.authorize("tool:write", Resource(kind="tool", tenant_id="demo"), _ctx(["tool:read"]))
    )
    assert missing.allowed is False
    assert missing.reason == "insufficient_scope"

    cross = asyncio.run(
        policy.authorize(
            "tool:read", Resource(kind="tool", tenant_id="other"), _ctx(["tool:read"])
        )
    )
    assert cross.allowed is False
    assert cross.reason == "cross_tenant_denied"


def test_rbac_denies_by_default_without_scopes() -> None:
    policy = RbacPolicy(_SECURITY)
    decision = asyncio.run(
        policy.authorize("retrieve", Resource(kind="doc", tenant_id="demo"), _ctx([]))
    )
    assert decision.allowed is False


def test_build_auth_context_maps_oidc_claims() -> None:
    ctx = build_auth_context(
        {
            "active": True,
            "sub": "alice",
            "tenant_id": "tenant-a",
            "realm_access": {"roles": ["support"]},
            "scope": "retrieve tool:write",
            "groups": ["public", "support"],
            "jti": "req-1",
        },
        _SECURITY,
    )

    assert ctx.tenant_id == "tenant-a"
    assert ctx.user_id == "alice"
    assert ctx.roles == ["support"]
    assert ctx.scopes == ["retrieve", "tool:read", "tool:write"]
    assert ctx.acl_tokens == ["public", "support"]
    assert ctx.request_id == "req-1"
