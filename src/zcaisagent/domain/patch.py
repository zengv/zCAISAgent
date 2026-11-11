from dataclasses import dataclass, field
from typing import Any

from zcaisagent.domain.enums import AutonomyLevel, RunStatus
from zcaisagent.domain.models import (
    Answer,
    Budget,
    Evidence,
    Message,
    PendingApproval,
    Plan,
    ToolInvocation,
    Verdict,
)


@dataclass
class StatePatch:
    status: RunStatus | None = None
    level: AutonomyLevel | None = None
    plan: Plan | None = None
    answer: Answer | None = None
    verdict: Verdict | None = None
    budget: Budget | None = None
    error: dict[str, Any] | None = None
    analysis: dict[str, Any] | None = None
    append_evidence: list[Evidence] = field(default_factory=list)
    replace_evidence: list[Evidence] | None = None
    append_notes: list[str] = field(default_factory=list)
    append_invocations: list[ToolInvocation] = field(default_factory=list)
    append_message: Message | None = None
    incr_step: int = 0
    # 有效动作次数（由执行动作的节点自报，如 retrieve/act）；由 reducer 累加到 budget.used_actions。
    incr_actions: int = 0
    incr_repair: int = 0
    last_node: str | None = None
    pending_approval: PendingApproval | None = None
    clear_pending_approval: bool = False
    resume_node: str | None = None
