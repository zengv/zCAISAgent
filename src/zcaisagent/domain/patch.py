from dataclasses import dataclass, field
from typing import Any

from zcaisagent.domain.enums import AutonomyLevel, RunStatus
from zcaisagent.domain.models import Answer, Budget, Evidence, Message, Plan, Verdict


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
    append_message: Message | None = None
    incr_step: int = 0
    incr_repair: int = 0
    last_node: str | None = None
