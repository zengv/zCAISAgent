from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from zcaisagent.domain.enums import RunStatus
from zcaisagent.domain.models import RunState
from zcaisagent.domain.patch import StatePatch

NodeFn = Callable[[RunState, "RunContext"], Awaitable[StatePatch]]
Condition = Callable[[RunState], bool]


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    condition: Condition | None = None
    label: str = ""


@dataclass
class GraphSpec:
    nodes: dict[str, NodeFn]
    edges: list[Edge]
    entry: str
    terminals: dict[str, RunStatus] = field(default_factory=dict)

    def next_node(self, state: RunState) -> str:
        current = state.last_node
        if current is None:
            return self.entry
        for edge in self.edges:
            if edge.src != current:
                continue
            if edge.condition is None or edge.condition(state):
                return edge.dst
        return "__dead_end__"
