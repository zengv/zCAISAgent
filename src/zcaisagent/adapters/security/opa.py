"""OPA 策略适配器：POST 决策请求，异常或非法响应一律拒绝。"""

from __future__ import annotations

import httpx

from zcaisagent.ports.security import AuthContext, Decision, Resource


class OpaPolicy:
    def __init__(
        self,
        url: str,
        timeout_s: float = 3.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url
        self.timeout_s = timeout_s
        self.transport = transport

    async def authorize(
        self, action: str, resource: Resource, ctx: AuthContext
    ) -> Decision:
        payload = {
            "input": {
                "action": action,
                "resource": resource.model_dump(),
                "context": ctx.model_dump(),
            }
        }
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=self.timeout_s
            ) as client:
                response = await client.post(self.url, json=payload)
            if response.status_code != 200:
                return Decision(allowed=False, reason="opa_unavailable")
            body = response.json()
        except (httpx.HTTPError, ValueError):
            return Decision(allowed=False, reason="opa_unavailable")

        result = body.get("result") if isinstance(body, dict) else None
        if result is True:
            return Decision(allowed=True, reason="ok")
        if result is False:
            return Decision(allowed=False, reason="opa_denied")
        return Decision(allowed=False, reason="opa_unavailable")
