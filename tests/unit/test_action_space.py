import pytest

from zcaisagent.orchestration.actions.registry import (
    ActionSpec,
    ActionSpace,
    build_default_action_space,
)


def test_default_space_covers_mvp_actions() -> None:
    space = build_default_action_space()
    names = {spec.name for spec in space.specs()}
    assert {"retrieve", "tool_call", "code_exec", "browse", "file_op", "finish"} <= names
    # ask_user 属于 P1，MVP 不可用。
    assert not space.available("ask_user")


def test_duplicate_action_registration_is_rejected() -> None:
    space = ActionSpace()
    space.register(ActionSpec("retrieve", "read", ("retrieve",)))
    with pytest.raises(ValueError):
        space.register(ActionSpec("retrieve", "read", ("retrieve",)))


def test_missing_scopes_are_reported() -> None:
    space = build_default_action_space()
    assert space.missing_scopes("retrieve", ["retrieve"]) == []
    assert space.missing_scopes("retrieve", []) == ["retrieve"]
    assert space.missing_scopes("unknown", ["retrieve"]) == ["<unregistered>"]
