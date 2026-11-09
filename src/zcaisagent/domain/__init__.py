"""Pure domain layer: models, enums, errors, events, state patches."""

from zcaisagent.domain.enums import (
    AutonomyLevel,
    NodeName,
    RunStatus,
    SourceType,
    Support,
)
from zcaisagent.domain.errors import ErrorInfo, ZCAISAgentError
from zcaisagent.domain.events import RunEvent
from zcaisagent.domain.mandate import Mandate, MandateStrength
from zcaisagent.domain.models import (
    Answer,
    Budget,
    Chunk,
    Citation,
    Claim,
    Document,
    Evidence,
    Message,
    Plan,
    PlanStep,
    RunState,
    Verdict,
)
from zcaisagent.domain.patch import StatePatch
from zcaisagent.domain.stage import TicketStage, ticket_stage

__all__ = [
    "Answer",
    "AutonomyLevel",
    "Budget",
    "Chunk",
    "Citation",
    "Claim",
    "Document",
    "ErrorInfo",
    "Evidence",
    "Message",
    "Mandate",
    "MandateStrength",
    "NodeName",
    "Plan",
    "PlanStep",
    "RunEvent",
    "RunState",
    "RunStatus",
    "SourceType",
    "StatePatch",
    "Support",
    "TicketStage",
    "Verdict",
    "ZCAISAgentError",
    "ticket_stage",
]
