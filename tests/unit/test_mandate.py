from pathlib import Path

from zcaisagent.container import build_container, create_state
from zcaisagent.domain.mandate import MandateStrength
from zcaisagent.ports.security import AuthContext
from zcaisagent.settings import Settings


def test_create_state_records_mandate_from_auth(tmp_path) -> None:
    container = build_container(
        Settings(data_dir=tmp_path, seed_dir=Path("data/seed"))
    )
    auth = AuthContext(
        tenant_id="demo",
        user_id="alice",
        roles=["admin"],
        scopes=["retrieve", "run:create"],
        acl_tokens=["public"],
    )
    state = create_state(container, "问题", auth=auth)
    assert state.mandate is not None
    assert state.mandate.user_id == "alice"
    assert state.mandate.tenant_id == "demo"
    assert state.mandate.strength == MandateStrength.EXPLICIT
    assert state.mandate.policy_version == "v1"
    assert state.mandate.scopes == ["retrieve", "run:create"]


def test_create_state_without_auth_has_no_mandate(tmp_path) -> None:
    container = build_container(
        Settings(data_dir=tmp_path, seed_dir=Path("data/seed"))
    )
    state = create_state(container, "问题")
    assert state.mandate is None
