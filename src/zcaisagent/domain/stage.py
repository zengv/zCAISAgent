from enum import StrEnum

from zcaisagent.domain.enums import RunStatus
from zcaisagent.domain.models import RunState


class TicketStage(StrEnum):
    QUEUED = "queued"
    WORKING = "working"
    PERSON_ANSWER = "person/answer"
    PERSON_REVIEW = "person/review"
    BLOCKED = "person/blocked"
    FAILED = "person/failed"
    BUDGET_EXCEEDED = "budget_exceeded"
    DONE = "done"
    ABSTAINED = "abstained"
    CANCELLED = "cancelled"


def ticket_stage(state: RunState) -> TicketStage:
    """Derive the stage from state instead of storing a second status."""
    if state.status == RunStatus.AWAITING_USER:
        return TicketStage.PERSON_ANSWER
    if state.status == RunStatus.FAILED:
        return TicketStage.FAILED
    if state.status == RunStatus.BUDGET_EXCEEDED:
        return TicketStage.BUDGET_EXCEEDED
    if state.status == RunStatus.CANCELLED:
        return TicketStage.CANCELLED
    if state.status == RunStatus.ABSTAINED:
        return TicketStage.ABSTAINED
    if state.status == RunStatus.SUCCEEDED:
        return TicketStage.DONE
    if state.last_node is None:
        return TicketStage.QUEUED
    if state.answer is not None and state.verdict is None:
        return TicketStage.PERSON_REVIEW
    if state.verdict is not None and not state.verdict.passed:
        return TicketStage.BLOCKED
    return TicketStage.WORKING
