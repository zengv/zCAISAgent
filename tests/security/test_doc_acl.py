import asyncio
from pathlib import Path

import pytest

from tests.conftest import make_state
from zcaisagent.container import build_container, build_context
from zcaisagent.orchestration.nodes.retrieve import retrieve_node
from zcaisagent.settings import N2SQLConfig, N2SQLUserScope, SecurityConfig, Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        seed_dir=Path("data/seed"),
        security=SecurityConfig(
            roles={
                "teacher": ["retrieve", "tool:read"],
                "student": ["retrieve", "tool:read"],
                "admin": ["retrieve", "tool:read"],
            },
            acl_tokens_by_role={
                "teacher": ["public"],
                "student": ["it_support"],
                "admin": ["public", "it_support"],
            },
        ),
        n2sql=N2SQLConfig(
            enabled=False,
            users={
                "admin": N2SQLUserScope("admin", "admin"),
                "teacher": N2SQLUserScope("teacher", "teacher", teacher_name="梁文锋"),
                "student": N2SQLUserScope("student", "student", student_id="S1"),
            },
        ),
        seed_acl_default=["public"],
        seed_acl_by_file={"it_support.md": ["it_support"]},
    )


@pytest.fixture(scope="module")
def container(tmp_path_factory):
    return build_container(_settings(tmp_path_factory.mktemp("doc-acl")))


def _titles(container, user_id: str, goal: str) -> list[str]:
    auth = asyncio.run(
        container.identity.authenticate(
            container.settings.api_key, {"user_id": user_id}
        )
    )
    ctx = build_context(container, auth)
    patch = asyncio.run(retrieve_node(make_state(goal), ctx))
    return [item.title or "" for item in patch.append_evidence]


def test_student_only_sees_it_support(container) -> None:
    vpn = _titles(container, "student", "VPN 连接怎么操作？")
    assert any("IT 支持手册" in title for title in vpn)

    policy = _titles(container, "student", "一线城市住宿标准是多少？")
    assert not any("差旅与报销" in title for title in policy)


def test_teacher_sees_docs_except_it_support(container) -> None:
    policy = _titles(container, "teacher", "一线城市住宿标准是多少？")
    assert any("差旅与报销" in title for title in policy)

    vpn = _titles(container, "teacher", "VPN 连接怎么操作？")
    assert not any("IT 支持手册" in title for title in vpn)


def test_admin_sees_all_seed_docs(container) -> None:
    vpn = _titles(container, "admin", "VPN 连接怎么操作？")
    assert any("IT 支持手册" in title for title in vpn)
    assert any(
        "差旅与报销" in title
        for title in _titles(container, "admin", "一线城市住宿标准是多少？")
    )


def test_unknown_user_sees_nothing(container) -> None:
    assert _titles(container, "nobody", "VPN 连接怎么操作？") == []
    assert _titles(container, "nobody", "一线城市住宿标准是多少？") == []
