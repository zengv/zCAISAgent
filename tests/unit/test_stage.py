from zcaisagent.domain.enums import RunStatus
from zcaisagent.domain.models import Answer, Verdict
from zcaisagent.domain.stage import TicketStage, ticket_stage
from tests.conftest import make_state


def _with(state, **updates):
    return state.model_copy(update=updates)


def test_fresh_state_is_queued() -> None:
    assert ticket_stage(make_state("问题")) == TicketStage.QUEUED


def test_running_state_is_working() -> None:
    state = _with(make_state("问题"), last_node="retrieve", step=2)
    assert ticket_stage(state) == TicketStage.WORKING


def test_answer_without_verdict_is_review() -> None:
    state = _with(
        make_state("问题"),
        last_node="synthesize",
        step=4,
        answer=Answer(text="草稿"),
    )
    assert ticket_stage(state) == TicketStage.PERSON_REVIEW


def test_failed_verdict_is_blocked() -> None:
    state = _with(
        make_state("问题"),
        last_node="verify",
        step=5,
        answer=Answer(text="草稿"),
        verdict=Verdict(passed=False),
    )
    assert ticket_stage(state) == TicketStage.BLOCKED


def test_terminal_statuses_map_to_stages() -> None:
    assert ticket_stage(_with(make_state("问题"), status=RunStatus.SUCCEEDED)) == TicketStage.DONE
    assert (
        ticket_stage(_with(make_state("问题"), status=RunStatus.ABSTAINED))
        == TicketStage.ABSTAINED
    )
    assert (
        ticket_stage(_with(make_state("问题"), status=RunStatus.AWAITING_USER))
        == TicketStage.PERSON_ANSWER
    )
    assert (
        ticket_stage(_with(make_state("问题"), status=RunStatus.BUDGET_EXCEEDED))
        == TicketStage.BUDGET_EXCEEDED
    )
