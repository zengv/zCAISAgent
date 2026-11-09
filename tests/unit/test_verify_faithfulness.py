import asyncio

from tests.conftest import make_state
from zcaisagent.domain.models import Answer, Claim, Evidence
from zcaisagent.orchestration.nodes.verify import verify_node


def test_faithfulness_is_measured_independently_from_citation_support() -> None:
    evidence = Evidence(
        id="ev1",
        source_name="travel_policy",
        title="差旅与报销管理办法",
        content="一线城市住宿标准为 800 元/晚，出差结束后 30 天内提交报销。",
    )
    claim = Claim(
        id="c1",
        text="一线城市住宿标准为 800 元/晚。",
        evidence_ids=["ev1"],
    )
    state = make_state("一线城市住宿标准是多少？").model_copy(
        update={
            "evidence": [evidence],
            "answer": Answer(
                text="一线城市住宿标准为 800 元/晚。另有 999 元的隐形福利。",
                claims=[claim],
            ),
        }
    )
    patch = asyncio.run(verify_node(state, None))
    verdict = patch.verdict
    assert verdict.citation_support == 1.0
    # 第二句的内容词元在证据中几乎没有覆盖，faithfulness 应低于引用支持度。
    assert verdict.faithfulness == 0.5


def test_citation_support_catches_number_mismatch_that_faithfulness_misses() -> None:
    evidence = Evidence(
        id="ev1",
        source_name="hr_leave",
        title="假期管理办法",
        content="工作满 1 年不满 10 年的员工享有 5 天年假。",
    )
    claim = Claim(
        id="c1",
        text="工作满 1 年不满 10 年享有 8 天年假。",
        evidence_ids=["ev1"],
    )
    state = make_state("年假几天？").model_copy(
        update={
            "evidence": [evidence],
            "answer": Answer(text="工作满 1 年不满 10 年享有 8 天年假。", claims=[claim]),
        }
    )
    patch = asyncio.run(verify_node(state, None))
    # 正文词元与证据高度重合，faithfulness 仍判为有支撑；
    # 但 claim 中的数字 8 与证据 5 不一致，citation_support 会降低。
    assert patch.verdict.citation_support == 0.0
    assert patch.verdict.faithfulness == 1.0
