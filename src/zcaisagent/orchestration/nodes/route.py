from zcaisagent.domain.models import RunState
from zcaisagent.domain.patch import StatePatch
from zcaisagent.orchestration.runtime import RunContext


async def route_node(state: RunState, ctx: RunContext) -> StatePatch:
    # 路由决策由 analyze 写入 analysis；本节点只做显式记录，便于轨迹回放。
    intent = state.analysis.get("intent", "查事实")
    basis = state.analysis.get("basis", "rule")
    return StatePatch(
        analysis={"route_basis": basis, "route_level": state.level.value},
        append_notes=[f"route:{state.level.value}:{basis}:{intent}"],
    )
