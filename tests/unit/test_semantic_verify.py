import asyncio
import dataclasses

from tests.conftest import make_ctx, make_state
from zcaisagent.adapters.llm.fake import FakeLLM
from zcaisagent.domain.models import Answer, Claim, Evidence
from zcaisagent.orchestration.nodes.verify import verify_node
from zcaisagent.orchestration.policies.verify_judge import LLMEntailmentJudge
from zcaisagent.ports.evaluation import EntailmentResult
from zcaisagent.ports.llm import LLMResponse
from zcaisagent.settings import EvaluatorConfig


def _state():
    evidence = Evidence(
        id="e1", source_name="inmemory", content="一线城市住宿标准为 800 元/晚。"
    )
    answer = Answer(
        text="一线城市住宿标准为 800 元/晚。",
        claims=[
            Claim(
                id="c0",
                text="一线城市住宿标准为 800 元/晚。",
                evidence_ids=["e1"],
            )
        ],
    )
    return make_state("一线城市住宿标准是多少？").model_copy(
        update={"evidence": [evidence], "answer": answer}
    )


class _StubVerifier:
    def __init__(self, score, fail=False):
        self.score = score
        self.fail = fail

    async def entail(self, claim, evidence):
        if self.fail:
            raise RuntimeError("judge down")
        return EntailmentResult(
            score=self.score,
            response=LLMResponse(text="{}", tokens_in=3, tokens_out=2),
        )


def _ctx(score, *, enabled=True, fail_open=True, min_ent=0.8, fail=False):
    ctx = make_ctx()
    ctx.config = dataclasses.replace(
        ctx.config,
        evaluator=EvaluatorConfig(
            semantic_verify_enabled=enabled,
            min_entailment=min_ent,
            fail_open=fail_open,
        ),
    )
    ctx.verifier = _StubVerifier(score, fail=fail)
    return ctx


def test_semantic_disabled_matches_lexical() -> None:
    patch = asyncio.run(verify_node(_state(), _ctx(0.1, enabled=False)))
    assert patch.verdict and patch.verdict.passed
    assert patch.budget is None  # 未启用判官不产生预算变更


def test_semantic_high_score_passes() -> None:
    patch = asyncio.run(verify_node(_state(), _ctx(0.95)))
    assert patch.verdict and patch.verdict.passed
    assert any("semantic=0.95" in c.note for c in patch.answer.claims)
    assert patch.budget is not None and patch.budget.used_llm_calls == 1


def test_semantic_low_score_blocks() -> None:
    patch = asyncio.run(verify_node(_state(), _ctx(0.1)))
    assert patch.verdict and not patch.verdict.passed
    assert patch.answer.claims[0].support == "partial"


def test_semantic_fail_open_falls_back_to_lexical() -> None:
    patch = asyncio.run(verify_node(_state(), _ctx(None, fail_open=True)))
    assert patch.verdict and patch.verdict.passed
    assert any("semantic=unavailable" in c.note for c in patch.answer.claims)


def test_semantic_judge_exception_fail_open() -> None:
    patch = asyncio.run(verify_node(_state(), _ctx(0.0, fail=True)))
    assert patch.verdict and patch.verdict.passed


def test_fake_llm_entailment_judge() -> None:
    judge = LLMEntailmentJudge(FakeLLM())
    result = asyncio.run(judge.entail("一线住宿 800", ["一线城市住宿标准为 800 元/晚。"]))
    assert result.score is not None and result.score >= 0.8
