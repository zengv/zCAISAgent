from zcaisagent.domain.enums import RunStatus
from zcaisagent.domain.models import Answer, RunState
from zcaisagent.domain.patch import StatePatch
from zcaisagent.orchestration.runtime import RunContext


async def respond_node(state: RunState, ctx: RunContext) -> StatePatch:
    if state.answer is None:
        return StatePatch(
            status=RunStatus.ABSTAINED,
            answer=Answer(text="未生成有效答案。", abstained=True, confidence="low"),
        )
    return StatePatch(status=RunStatus.SUCCEEDED, append_notes=["responded"])
