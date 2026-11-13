from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from zcaisagent.domain.models import Evidence
from zcaisagent.ports.llm import LLMResponse


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


class EntailmentResult(BaseModel):
    """单条 claim 的语义蕴含结果；score 为 None 表示判官不可用（可回退词面）。"""

    score: float | None = None
    response: LLMResponse | None = None
    # 判官调用的轨迹留痕（prompt、原始响应、耗时），由实现方填充。
    call: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class EvaluatorPort(Protocol):
    """证据评分 / 充分性判定端口（离线策略，同步纯计算）。

    默认实现为 ``orchestration/policies/evidence.py::RuleEvaluator``（本地确定性
    规则）；可替换为向量/LLM judge 实现。答案级校验不在此端口内（由 ``verify``
    节点承担）。
    """

    def grade_evidence(
        self,
        question: str,
        evidence: list[Evidence],
        rerank_scores: dict[str, float] | None = None,
    ) -> list[EvidenceGrade]: ...

    def judge_sufficiency(
        self, question: str, evidence: list[Evidence], grades: list[EvidenceGrade]
    ) -> Sufficiency: ...


@runtime_checkable
class AnswerVerifierPort(Protocol):
    """答案级语义校验端口（NLI judge）；默认实现复用 LLMPort，可由容器按需注入。"""

    async def entail(self, claim: str, evidence: list[str]) -> EntailmentResult: ...
