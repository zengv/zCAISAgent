from typing import Protocol

from pydantic import BaseModel, Field

from zcaisagent.domain.models import Evidence


class AuthContext(BaseModel):
    tenant_id: str
    user_id: str
    roles: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    acl_tokens: list[str] = Field(default_factory=lambda: ["public"])
    request_id: str = ""


class Resource(BaseModel):
    kind: str
    id: str = ""
    tenant_id: str = ""


class Decision(BaseModel):
    allowed: bool
    reason: str = ""
    obligations: dict = Field(default_factory=dict)


class GuardrailResult(BaseModel):
    allowed: bool = True
    category: str | None = None
    sanitized_text: str | None = None
    details: dict = Field(default_factory=dict)


class AuditEvent(BaseModel):
    action: str
    actor: str
    tenant_id: str
    run_id: str = ""
    detail: dict = Field(default_factory=dict)


class IdentityPort(Protocol):
    async def authenticate(self, token: str, request_meta: dict) -> AuthContext: ...


class PolicyPort(Protocol):
    async def authorize(
        self, action: str, resource: Resource, ctx: AuthContext
    ) -> Decision: ...


class GuardrailPort(Protocol):
    async def check_input(self, text: str, ctx: AuthContext) -> GuardrailResult: ...

    async def check_output(self, text: str, ctx: AuthContext) -> GuardrailResult: ...

    async def check_evidence(
        self, evidence: Evidence, ctx: AuthContext
    ) -> GuardrailResult: ...


class AuditPort(Protocol):
    async def append(self, event: AuditEvent) -> None: ...


class SecretPort(Protocol):
    async def get(self, key: str) -> str: ...
