from zcaisagent.domain.errors import ZCAISAgentError
from zcaisagent.ports.security import AuthContext, Decision, Resource


class ApiKeyIdentity:
    def __init__(self, api_key: str, tenant_id: str = "demo") -> None:
        self.api_key = api_key
        self.tenant_id = tenant_id

    async def authenticate(self, token: str, request_meta: dict) -> AuthContext:
        if not token or token != self.api_key:
            raise ZCAISAgentError("UNAUTHORIZED", "invalid api key")
        return AuthContext(
            tenant_id=request_meta.get("tenant_id", self.tenant_id),
            user_id=request_meta.get("user_id", "dev"),
            roles=["admin"],
            scopes=["retrieve", "tool:read", "run:create"],
            acl_tokens=["public"],
            request_id=request_meta.get("request_id", ""),
        )


class RbacPolicy:
    ALLOWED = {"retrieve", "tool:read", "run:create"}

    async def authorize(
        self, action: str, resource: Resource, ctx: AuthContext
    ) -> Decision:
        if action not in self.ALLOWED:
            return Decision(allowed=False, reason="action_not_allowed")
        if resource.tenant_id and resource.tenant_id != ctx.tenant_id:
            return Decision(allowed=False, reason="cross_tenant_denied")
        return Decision(allowed=True, reason="ok")
