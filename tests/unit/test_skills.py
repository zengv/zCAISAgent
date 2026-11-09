import asyncio

import pytest

from tests.conftest import make_ctx, make_state
from zcaisagent.capabilities.skills.registry import (
    InMemorySkillRegistry,
    build_default_skill_registry,
)
from zcaisagent.domain.models import Answer, Claim, Evidence, Skill
from zcaisagent.orchestration.nodes.verify import verify_node


def test_enterprise_skill_matches_by_keyword() -> None:
    decision = build_default_skill_registry().match(
        "年假天数是多少？", "查事实", ["retrieve"]
    )
    assert decision.skill is not None
    assert decision.skill.name == "enterprise_knowledge_qa"
    assert decision.reason == "keyword:年假"


def test_unmatched_question_falls_back_to_generic_policy() -> None:
    decision = build_default_skill_registry().match(
        "今天天气怎么样？", "闲聊", ["retrieve"]
    )
    assert decision.skill is None
    assert decision.reason == "intent_not_applicable:闲聊"


def test_missing_scope_rejects_skill_without_partial_load() -> None:
    decision = build_default_skill_registry().match(
        "年假天数是多少？", "查事实", []
    )
    assert decision.skill is None
    assert decision.reason == "permission_denied:retrieve"


def test_duplicate_skill_registration_is_rejected() -> None:
    registry = InMemorySkillRegistry()
    skill = Skill(name="demo", version="1.0.0")
    registry.register(skill)
    with pytest.raises(ValueError):
        registry.register(skill)


def _verify_state(min_support: float | None):
    evidence = Evidence(
        id="e1",
        source_name="inmemory",
        content="一线城市住宿标准为 800 元/晚。",
        score=0.8,
        relevance=0.8,
    )
    supported = "一线城市住宿标准为 800 元/晚。"
    answer = Answer(
        text=supported,
        claims=[
            Claim(id="c0", text=supported, evidence_ids=["e1"]),
            Claim(id="c1", text=supported, evidence_ids=["e1"]),
            Claim(id="c2", text="完全无关的表述XYZ", evidence_ids=["e1"]),
        ],
    )
    skill = {"matched": True, "min_citation_support": min_support}
    return make_state("住宿标准是多少？").model_copy(
        update={
            "evidence": [evidence],
            "answer": answer,
            "analysis": {"skill": skill},
        }
    )


def test_skill_policy_tightens_citation_threshold() -> None:
    baseline = asyncio.run(verify_node(_verify_state(None), make_ctx()))
    tightened = asyncio.run(verify_node(_verify_state(0.7), make_ctx()))
    assert baseline.verdict is not None and baseline.verdict.passed
    assert tightened.verdict is not None
    assert not tightened.verdict.passed
    assert "insufficient_citation_support" in tightened.verdict.issues
