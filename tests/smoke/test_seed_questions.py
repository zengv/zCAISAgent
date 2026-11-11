import asyncio
from pathlib import Path

import pytest

from zcaisagent.container import build_container, build_context, create_state
from zcaisagent.domain.enums import AutonomyLevel
from zcaisagent.orchestration.build import build_default_graph
from zcaisagent.orchestration.runtime import Runtime
from zcaisagent.settings import Settings

QUESTIONS = [
    "一线城市住宿标准是多少？",
    "二线城市住宿标准是多少？",
    "其他城市住宿标准是多少？",
    "出差补贴一天多少钱？",
    "出差结束后多久内要提交报销？",
    "报销需要提交哪些材料？",
    "报销审批需要几个工作日？",
    "高铁出行应该选什么座位？",
    "公司 VPN 用什么客户端？",
    "VPN 连接地址是什么？",
    "密码重置需要多久完成？",
    "临时密码有效期多久？",
    "笔记本电脑多久可以更换？",
    "企业邮箱容量是多少？",
    "离职时需要在几天内归还设备？",
    "工作满 1 年不满 10 年有几天年假？",
    "年假可以延到什么时候？",
    "病假工资按什么比例发放？",
    "婚假有几天？",
    "陪产假多少天？",
]


@pytest.fixture(scope="module")
def container(tmp_path_factory):
    settings = Settings(
        data_dir=tmp_path_factory.mktemp("zcaisagent"),
        seed_dir=Path("data/seed"),
        top_k=5,
    )
    return build_container(settings)


@pytest.mark.parametrize("question", QUESTIONS)
def test_seed_questions_are_answerable(container, question: str) -> None:
    auth = asyncio.run(
        container.identity.authenticate(
            container.settings.api_key, {"tenant_id": "demo"}
        )
    )

    async def run():
        state = create_state(container, goal=question, level=AutonomyLevel.L1_FAST)
        ctx = build_context(container, auth)
        return await Runtime().execute(build_default_graph(), state, ctx)

    state = asyncio.run(run())
    assert state.status.value == "succeeded", f"{question} -> {state.status}"
    assert state.evidence, question
    assert state.answer and state.answer.text
