from typing import Protocol

from zcaisagent.domain.models import Answer, Evidence
from zcaisagent.domain.models import Verdict
from pydantic import BaseModel, Field


class EvidenceGrade(BaseModel):
    evidence_id: str
    relevance: float = 0.0
    authority: float = 0.0
    freshness: float = 0.0
    consistency: float = 0.0
    final: float = 0.0
    decision: str = "keep"


class Sufficiency(BaseModel):
    coverage: float = 0.0
    missing_aspects: list[str] = Field(default_factory=list)
    decision: str = "insufficient"


class EvaluatorPort(Protocol):
    async def grade_evidence(
        self, question: str, evidence: list[Evidence]
    ) -> EvidenceGrade: ...

    async def judge_sufficiency(
        self, question: str, evidence: list[Evidence]
    ) -> Sufficiency: ...

    async def verify_answer(
        self, answer: Answer, evidence: list[Evidence]
    ) -> Verdict: ...
