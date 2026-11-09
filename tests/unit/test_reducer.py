from zcaisagent.domain.enums import RunStatus
from zcaisagent.domain.models import Evidence, Message
from zcaisagent.orchestration.reducers import apply_patch
from zcaisagent.domain.patch import StatePatch
from tests.conftest import make_state


def test_apply_patch_appends_and_dedupes_evidence() -> None:
    state = make_state("测试")
    evidence = Evidence(id="ev1", source_name="test", content="A")
    state = apply_patch(state, StatePatch(append_evidence=[evidence], incr_step=1))
    state = apply_patch(state, StatePatch(append_evidence=[evidence], incr_step=1))
    assert len(state.evidence) == 1
    assert state.step == 2
    assert state.last_node is None


def test_apply_patch_replaces_evidence_and_status() -> None:
    state = make_state("测试")
    state = apply_patch(
        state,
        StatePatch(
            append_evidence=[
                Evidence(id="ev1", source_name="test", content="A"),
                Evidence(id="ev2", source_name="test", content="B"),
            ]
        ),
    )
    state = apply_patch(
        state,
        StatePatch(
            replace_evidence=[Evidence(id="ev3", source_name="test", content="C")],
            status=RunStatus.ABSTAINED,
        ),
    )
    assert [e.id for e in state.evidence] == ["ev3"]
    assert state.status == RunStatus.ABSTAINED


def test_apply_patch_appends_message_and_notes() -> None:
    state = make_state("测试")
    state = apply_patch(
        state,
        StatePatch(
            append_message=Message(role="assistant", content="hi"),
            append_notes=["n1"],
        ),
    )
    assert state.messages[-1].content == "hi"
    assert state.working_notes == ["n1"]
