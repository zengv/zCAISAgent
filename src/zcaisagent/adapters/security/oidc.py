"""OIDC 身份适配器：通过 token introspection 把 claims 映射为 AuthContext。"""

from __future__ import annotations

import httpx

from zcaisagent.domain.errors import ZCAISAgentError
from zcaisagent.governance.identity import build_auth_context
from zcaisagent.ports.security import AuthContext
from zcaisagent.settings import SecurityConfig


class OidcIdentity:
    """RFC 7662 introspection 客户端。

    客户端密钥由调用方从环境变量读取后注入，不落盘、不入日志。
    """

    def __init__(
        self,
        introspection_url: str,
        client_id: str,
        client_secret: str,
        timeout_s: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
        security: SecurityConfig | None = None,
    ) -> None:
        self.introspection_url = introspection_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout_s = timeout_s
        self.transport = transport
        self.security = security

    async def authenticate(self, token: str, request_meta: dict) -> AuthContext:
        if not token:
            raise ZCAISAgentError("UNAUTHORIZED", "invalid token")
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=self.timeout_s
            ) as client:
                response = await client.post(
                    self.introspection_url,
                    data={
                        "token": token,
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                )
        except httpx.HTTPError as exc:
            raise ZCAISAgentError("UNAUTHORIZED", "invalid token") from exc

        if response.status_code != 200:
            raise ZCAISAgentError("UNAUTHORIZED", "invalid token")
        try:
            claims = response.json()
        except ValueError as exc:
            raise ZCAISAgentError("UNAUTHORIZED", "invalid token") from exc
        if not isinstance(claims, dict) or not claims.get("active"):
            raise ZCAISAgentError("UNAUTHORIZED", "invalid token")

        ctx = build_auth_context(claims, self.security)
        # claims 中的租户/用户不可被请求体覆盖，避免越权；仅在 claim 缺失时兜底。
        if not ctx.tenant_id:
            ctx = ctx.model_copy(
                update={"tenant_id": str(request_meta.get("tenant_id") or "")}
            )
        if not ctx.user_id:
            ctx = ctx.model_copy(
                update={"user_id": str(request_meta.get("user_id") or "")}
            )
        return ctx
