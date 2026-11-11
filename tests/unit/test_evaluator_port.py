import asyncio

from tests.conftest import make_ctx, make_state
from zcaisagent.domain.models import Evidence
from zcaisagent.orchestration.nodes.grade import grade_node
from zcaisagent.orchestration.policies.evidence import (
    RuleEvaluator,
    grade_evidence,
    judge_sufficiency,
)
from zcaisagent.ports.evaluation import EvaluatorPort, EvidenceGrade


def _evidence() -> list[Evidence]:
    return [
        Evidence(
            id="e1",
            source_name="inmemory",
            title="差旅制度",
            content="一线城市住宿标准为 800 元/晚。",
        )
    ]


def test_rule_evaluator_matches_policy_functions() -> None:
    ev = _evidence()
    evaluator = RuleEvaluator()

    grades = evaluator.grade_evidence("一线城市住宿标准是多少？", ev)
    assert grades == grade_evidence("一线城市住宿标准是多少？", ev)
    assert isinstance(grades, list) and isinstance(grades[0], EvidenceGrade)

    suff = evaluator.judge_sufficiency("一线城市住宿标准是多少？", ev, grades)
    assert suff == judge_sufficiency("一线城市住宿标准是多少？", ev, grades)


def test_rule_evaluator_satisfies_port() -> None:
    # 结构性契约：默认实现符合 EvaluatorPort 协议
    assert isinstance(RuleEvaluator(), EvaluatorPort)


def test_grade_node_prefers_injected_evaluator() -> None:
    class _SpyEvaluator:
        def __init__(self) -> None:
            self.grade_calls = 0
            self.suff_calls = 0

        def grade_evidence(self, question, evidence, rerank_scores=None):
            self.grade_calls += 1
            return grade_evidence(question, evidence, rerank_scores=rerank_scores)

        def judge_sufficiency(self, question, evidence, grades):
            self.suff_calls += 1
            return judge_sufficiency(question, evidence, grades)

    ctx = make_ctx()
    spy = _SpyEvaluator()
    ctx.evaluator = spy
    state = make_state("一线城市住宿标准是多少？")
    state = state.model_copy(update={"evidence": _evidence()})

    patch = asyncio.run(grade_node(state, ctx))
    assert spy.grade_calls == 1 and spy.suff_calls == 1
    assert "sufficiency" in (patch.analysis or {})
