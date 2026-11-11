import asyncio
from types import SimpleNamespace

from tests.conftest import make_state
from zcaisagent.domain.enums import AutonomyLevel
from zcaisagent.domain.models import Evidence
from zcaisagent.orchestration.nodes.synthesize import (
    _max_tokens,
    _parse_answer,
    _timeout_s,
    synthesize_node,
)
from zcaisagent.ports.llm import LLMResponse


def test_parse_answer_recovers_truncated_json() -> None:
    text = (
        '{"answer":"应赔偿 3750 元。","claims":['
        '{"text":"未履行 15 个月，应赔偿 3750 元。","evidence_ids":["ev1"]},'
        '{"text":"服务期 24 个月'
    )
    answer = _parse_answer(text)
    assert answer.text == "应赔偿 3750 元。"
    assert [claim.text for claim in answer.claims] == ["未履行 15 个月，应赔偿 3750 元。"]
    assert answer.claims[0].evidence_ids == ["ev1"]


class _QueueLLM:
    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        text = self.texts.pop(0)
        return LLMResponse(text=text, tokens_in=10, tokens_out=10)


def test_empty_output_triggers_one_retry() -> None:
    llm = _QueueLLM(
        [
            "",
            '{"answer":"应为 800 元。","claims":['
            '{"text":"一线城市住宿标准为 800 元。","evidence_ids":["ev1"]}]}',
        ]
    )
    ctx = SimpleNamespace(
        llm=llm, config=SimpleNamespace(max_evidence_in_context=2, model=None)
    )
    state = make_state("一线城市住宿标准是多少？").model_copy(
        update={
            "evidence": [
                Evidence(
                    id="ev1",
                    source_name="travel_policy",
                    title="差旅与报销管理办法",
                    content="一线城市住宿标准为 800 元/晚。",
                )
            ]
        }
    )
    patch = asyncio.run(synthesize_node(state, ctx))
    assert len(llm.requests) == 2
    assert llm.requests[0].max_tokens == 2048
    assert llm.requests[1].max_tokens == 4096
    assert patch.answer.claims[0].text == "一线城市住宿标准为 800 元。"
    assert "synthesize_retry:empty" in patch.append_notes
    assert patch.budget.used_llm_calls == 2


def test_timeouts_are_tiered_by_level() -> None:
    # 默认基线 30s：L1/L2 不变，L3 翻倍到 60s；基线可由 ZCAISAGENT_NODE_TIMEOUT_S 调整。
    assert _timeout_s(AutonomyLevel.L1_FAST) == 30.0
    assert _timeout_s(AutonomyLevel.L2_STANDARD) == 30.0
    assert _timeout_s(AutonomyLevel.L3_DEEP) == 60.0
    assert _timeout_s(AutonomyLevel.L3_DEEP, base=45.0) == 90.0


def test_max_tokens_are_tiered_by_level() -> None:
    assert _max_tokens(AutonomyLevel.L1_FAST) == 2048
    assert _max_tokens(AutonomyLevel.L2_STANDARD) == 2048
    assert _max_tokens(AutonomyLevel.L3_DEEP) == 8192
    # 重试额度必须大于首次，否则只是再撞一次同样的上限。
    assert _max_tokens(AutonomyLevel.L1_FAST, retry=True) == 4096
    assert _max_tokens(AutonomyLevel.L2_STANDARD, retry=True) == 4096
    assert _max_tokens(AutonomyLevel.L3_DEEP, retry=True) == 16384
