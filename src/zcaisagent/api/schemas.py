from pydantic import BaseModel, Field

from zcaisagent.ports.security import AuthContext


class RunRequest(BaseModel):
    goal: str = Field(min_length=1)
    thread_id: str | None = None
    tenant_id: str | None = None
    user_id: str = "dev"
    level: str | None = None


class CitationOut(BaseModel):
    evidence_id: str
    quote: str | None = None
    confidence: float = 0.0


class RunResponse(BaseModel):
    run_id: str
    thread_id: str
    status: str
    stage: str
    answer: str
    confidence: str
    abstained: bool
    citations: list[CitationOut] = Field(default_factory=list)
    evidence_count: int = 0
    steps: int = 0
    mandate_id: str | None = None
    limitations: list[str] = Field(default_factory=list)


def auth_meta(request: RunRequest) -> dict:
    return {
        "tenant_id": request.tenant_id or "",
        "user_id": request.user_id,
    }


def ensure_tenant(auth: AuthContext, request: RunRequest) -> str:
    return request.tenant_id or auth.tenant_id
