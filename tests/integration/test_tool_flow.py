import asyncio

from zcaisagent.capabilities.tools.gateway import build_default_tool_gateway
from zcaisagent.domain.enums import RunStatus, SourceType
from zcaisagent.ports.security import AuthContext
from tests.conftest import make_chunk, make_ctx, make_state, run_graph


def _tool_ctx(chunks: list) -> object:
    ctx = make_ctx(chunks=chunks)
    ctx.auth = AuthContext(
        tenant_id="demo",
        user_id="tester",
        roles=["admin"],
        scopes=["retrieve", "tool:read"],
        acl_tokens=["public"],
    )
    ctx.tools = build_default_tool_gateway(retriever=ctx.retriever)
    return ctx


def test_tool_call_runs_end_to_end() -> None:
    ctx = _tool_ctx([make_chunk("kb#0", "专业软件安装需提交 IT 审批，审批通过后由 IT 部门安装。")])
    state = asyncio.run(run_graph(ctx, make_state("专业软件安装怎么申请？")))

    assert any(note == "action:s1:tool_call:1" for note in state.working_notes)
    tool_evidence = [item for item in state.evidence if item.source_type == SourceType.TOOL]
    assert tool_evidence, "工具结果未转为证据"
    assert tool_evidence[0].source_name == "kb_search"
    assert state.status in {RunStatus.SUCCEEDED, RunStatus.ABSTAINED}
    if state.status == RunStatus.SUCCEEDED:
        assert state.answer is not None and state.answer.text


def test_tool_unavailable_marks_action_error() -> None:
    ctx = _tool_ctx([make_chunk("kb#0", "专业软件安装需提交 IT 审批。")])
    ctx.tools = build_default_tool_gateway()
    state = asyncio.run(run_graph(ctx, make_state("专业软件安装怎么申请？")))

    assert any(note == "action:s1:tool_call:0" for note in state.working_notes)
    assert not [item for item in state.evidence if item.source_type == SourceType.TOOL]
