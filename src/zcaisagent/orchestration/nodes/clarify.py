from zcaisagent.domain.enums import RunStatus
from zcaisagent.domain.models import Answer, RunState
from zcaisagent.domain.patch import StatePatch
from zcaisagent.orchestration.runtime import RunContext

_GUIDE = "为了准确回答，请先确认："
_DEFAULT_QUESTION = "您问的是哪一项？"


def _render(clarify: dict) -> str:
    lines = [_GUIDE]
    for item in clarify.get("questions") or []:
        if not isinstance(item, dict):
            continue
        lines.append(str(item.get("question") or _DEFAULT_QUESTION))
        index = 0
        for option in item.get("options") or []:
            if isinstance(option, str) and option.strip():
                index += 1
                lines.append(f"{index}. {option.strip()}")
    return "\n".join(lines)


async def clarify_node(state: RunState, ctx: RunContext) -> StatePatch:
    """输出澄清追问并暂停等待用户补充；不调用模型、不检索、不增加动作。"""
    clarify = state.analysis.get("clarify")
    if not isinstance(clarify, dict) or not clarify.get("questions"):
        return StatePatch(status=RunStatus.AWAITING_USER)
    return StatePatch(
        status=RunStatus.AWAITING_USER,
        answer=Answer(text=_render(clarify), confidence="low", abstained=False),
        append_notes=["clarify:asked"],
    )
