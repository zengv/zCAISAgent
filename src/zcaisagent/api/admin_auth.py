"""Admin 路由鉴权：API Key 认证 + admin:read / admin:write scope。"""

from __future__ import annotations

from fastapi import Header, HTTPException, Request

from zcaisagent.api.deps import get_container
from zcaisagent.container import Container
from zcaisagent.domain.errors import ZCAISAgentError
from zcaisagent.ports.security import AuditEvent, AuthContext

ADMIN_READ = "admin:read"
ADMIN_WRITE = "admin:write"


async def _audit_denied(
    container: Container,
    *,
    actor: str,
    tenant_id: str,
    action: str,
    path: str,
    reason: str,
) -> None:
    await container.audit.append(
        AuditEvent(
            action="admin.denied",
            actor=actor,
            tenant_id=tenant_id,
            detail={"action": action, "reason": reason, "path": path},
        )
    )


async def _require(
    scope: str, action: str, api_key: str, path: str
) -> AuthContext:
    container = get_container()
    try:
        auth = await container.identity.authenticate(api_key, {})
    except ZCAISAgentError:
        await _audit_denied(
            container,
            actor="anonymous",
            tenant_id="",
            action=action,
            path=path,
            reason="unauthorized",
        )
        raise
    if scope not in auth.scopes:
        await _audit_denied(
            container,
            actor=auth.user_id,
            tenant_id=auth.tenant_id,
            action=action,
            path=path,
            reason="insufficient_scope",
        )
        raise HTTPException(status_code=403, detail="insufficient scope")
    return auth


async def require_admin_read(
    request: Request,
    x_api_key: str = Header(default="dev-key", alias="X-API-Key"),
) -> AuthContext:
    return await _require(ADMIN_READ, "admin.read", x_api_key, request.url.path)


async def require_admin_write(
    request: Request,
    x_api_key: str = Header(default="dev-key", alias="X-API-Key"),
) -> AuthContext:
    return await _require(ADMIN_WRITE, "admin.write", x_api_key, request.url.path)
