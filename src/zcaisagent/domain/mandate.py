from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field


class MandateStrength(StrEnum):
    EXPLICIT = "explicit"
    INFERRED = "inferred"


class Mandate(BaseModel):
    """What authorizes a run: who, under which policy, with which scope."""

    id: str
    tenant_id: str
    user_id: str
    policy_version: str = "v1"
    strength: MandateStrength = MandateStrength.EXPLICIT
    scopes: list[str] = Field(default_factory=list)
    acl_tokens: list[str] = Field(default_factory=lambda: ["public"])
    basis: str = "api_request"
    issued_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
