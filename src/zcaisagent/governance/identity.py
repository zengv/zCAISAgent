from typing import Any

from zcaisagent.domain.errors import ZCAISAgentError
from zcaisagent.ports.security import AuthContext, Decision, Resource
from zcaisagent.settings import SecurityConfig

DEFAULT_ROLE = "admin"
DEFAULT_SCOPES = ["retrieve", "tool:read", "run:create", "admin:read", "admin:write"]
DEFAULT_ACL_TOKENS = ["public"]


def _merge(target: list[str], values: list[str]) -> list[str]:
    for value in values:
        if value not in target:
            target.append(value)
    return target


def scopes_for_roles(
    roles: list[str], security: SecurityConfig | None
) -> list[str]:
    """按配置把角色映射为 scope；未声明角色（或未配置角色表）默认无 scope。

    未配置角色表时保留内置 admin 的最小可用 scope，兼容既有 API / CLI 入口。
    """
    if security is None or not security.roles:
        return list(DEFAULT_SCOPES) if DEFAULT_ROLE in roles else []
    scopes: list[str] = []
    for role in roles:
        _merge(scopes, list(security.roles.get(role, [])))
    return scopes


def acl_tokens_for_roles(
    roles: list[str], security: SecurityConfig | None
) -> list[str]:
    """按配置合并角色可见的 ACL token；未配置映射时默认 public。"""
    if security is None or not security.acl_tokens_by_role:
        return list(DEFAULT_ACL_TOKENS) if DEFAULT_ROLE in roles else []
    tokens: list[str] = []
    for role in roles:
        _merge(tokens, list(security.acl_tokens_by_role.get(role, [])))
    return tokens


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item for item in value.split() if item]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    return []


def _claim_roles(claims: dict) -> list[str]:
    roles = _as_list(claims.get("roles"))
    realm = claims.get("realm_access")
    if isinstance(realm, dict):
        _merge(roles, _as_list(realm.get("roles")))
    return roles


def _claim_scopes(claims: dict) -> list[str]:
    return _as_list(claims.get("scope"))


def _claim_acl_tokens(claims: dict) -> list[str]:
    tokens: list[str] = []
    for key in ("acl", "acl_tokens", "groups"):
        _merge(tokens, _as_list(claims.get(key)))
    return tokens


def build_auth_context(
    claims: dict, security: SecurityConfig | None = None
) -> AuthContext:
    """把 OIDC introspection / JWT claims 映射为 AuthContext。

    支持 sub / user_id、tenant_id / tenant、roles / realm_access.roles、
    scope（空格分隔）、acl / acl_tokens / groups。
    """
    roles = _claim_roles(claims)
    scopes = scopes_for_roles(roles, security)
    _merge(scopes, _claim_scopes(claims))
    acl_tokens = acl_tokens_for_roles(roles, security)
    _merge(acl_tokens, _claim_acl_tokens(claims))
    if not acl_tokens:
        acl_tokens = list(DEFAULT_ACL_TOKENS)
    return AuthContext(
        tenant_id=str(claims.get("tenant_id") or claims.get("tenant") or ""),
        user_id=str(claims.get("sub") or claims.get("user_id") or ""),
        roles=roles,
        scopes=scopes,
        acl_tokens=acl_tokens,
        request_id=str(claims.get("request_id") or claims.get("jti") or ""),
    )


class ApiKeyIdentity:
    def __init__(
        self,
        api_key: str,
        tenant_id: str = "demo",
        security: SecurityConfig | None = None,
        roles: list[str] | None = None,
    ) -> None:
        self.api_key = api_key
        self.tenant_id = tenant_id
        self.security = security
        self.roles = list(roles) if roles is not None else [DEFAULT_ROLE]

    async def authenticate(self, token: str, request_meta: dict) -> AuthContext:
        if not token or token != self.api_key:
            raise ZCAISAgentError("UNAUTHORIZED", "invalid api key")
        return AuthContext(
            tenant_id=request_meta.get("tenant_id") or self.tenant_id,
            user_id=request_meta.get("user_id") or "dev",
            roles=list(self.roles),
            scopes=scopes_for_roles(self.roles, self.security),
            acl_tokens=acl_tokens_for_roles(self.roles, self.security),
            request_id=request_meta.get("request_id", ""),
        )


class RbacPolicy:
    """配置驱动的 RBAC：deny by default，并强制租户隔离。"""

    def __init__(self, security: SecurityConfig | None = None) -> None:
        self.security = security

    async def authorize(
        self, action: str, resource: Resource, ctx: AuthContext
    ) -> Decision:
        if action not in ctx.scopes:
            return Decision(allowed=False, reason="insufficient_scope")
        if resource.tenant_id and resource.tenant_id != ctx.tenant_id:
            return Decision(allowed=False, reason="cross_tenant_denied")
        return Decision(allowed=True, reason="ok")
