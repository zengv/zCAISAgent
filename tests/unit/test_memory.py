import asyncio

from tests.conftest import make_state
from zcaisagent.capabilities.memory.distill import distill_and_write, distill_episode
from zcaisagent.capabilities.memory.policies import (
    MemoryWriteGuard,
    extract_explicit_memory,
    prepare_write,
)
from zcaisagent.capabilities.memory.store import InMemoryMemory
from zcaisagent.domain.enums import MemoryKind, RunStatus
from zcaisagent.domain.models import Answer, Evidence
from zcaisagent.ports.memory import MemoryItem, MemoryQuery, MemorySource, MemoryWrite
from zcaisagent.ports.security import AuthContext


def _auth(user_id: str = "tester", tenant_id: str = "demo") -> AuthContext:
    return AuthContext(tenant_id=tenant_id, user_id=user_id)


def _item(content: str, kind: str = MemoryKind.SEMANTIC.value) -> MemoryItem:
    return MemoryItem(id=f"m_{abs(hash(content))}", kind=kind, content=content)


def test_extract_explicit_memory_chinese() -> None:
    assert extract_explicit_memory("记住：以后回答用表格。") == "以后回答用表格"
    assert extract_explicit_memory("请记住 我的偏好是简洁") == "我的偏好是简洁"
    assert extract_explicit_memory("以后都先给结论") == "先给结论"


def test_extract_explicit_memory_english() -> None:
    assert extract_explicit_memory("Remember: always cite the source") == "always cite the source"


def test_extract_explicit_memory_returns_none_without_signal() -> None:
    assert extract_explicit_memory("年假天数是多少？") is None


def test_tool_and_web_sources_are_rejected() -> None:
    guard = MemoryWriteGuard()
    for source in (MemorySource.TOOL, MemorySource.WEB):
        allowed, reason = guard.evaluate(
            MemoryWrite(item=_item("来自网页的偏好"), source=source, explicit=True),
            _auth(),
        )
        assert allowed is False
        assert reason == "MEMORY_SOURCE_REJECTED"


def test_pii_is_redacted_before_write() -> None:
    prepared = prepare_write(
        _item("联系我 13800138000"), MemorySource.USER, True, _auth()
    )
    assert prepared is not None
    assert "13800138000" not in prepared.content
    assert "[REDACTED_PHONE]" in prepared.content


def test_high_risk_content_is_rejected() -> None:
    guard = MemoryWriteGuard()
    allowed, reason = guard.evaluate(
        MemoryWrite(item=_item("我的密码是 abc123"), source=MemorySource.USER),
        _auth(),
    )
    assert allowed is False
    assert reason == "MEMORY_HIGH_RISK"
    assert prepare_write(_item("api_key: sk-1234567890"), MemorySource.USER, True, _auth()) is None


def test_duplicate_content_is_written_once() -> None:
    store = InMemoryMemory()
    item = _item("以后回答用表格")
    asyncio.run(store.write(item, _auth()))
    asyncio.run(store.write(item.model_copy(update={"id": "another"}), _auth()))
    assert len(store.all_items()) == 1


def test_working_memory_is_not_stored() -> None:
    store = InMemoryMemory()
    asyncio.run(store.write(_item("本轮临时笔记", MemoryKind.WORKING.value), _auth()))
    assert store.all_items() == []


def test_namespace_isolation_by_user_and_tenant() -> None:
    store = InMemoryMemory()
    asyncio.run(store.write(_item("alice 偏好表格"), _auth("alice")))
    asyncio.run(store.write(_item("bob 偏好列表"), _auth("bob")))
    asyncio.run(store.write(_item("其他租户偏好"), _auth("alice", "other")))

    alice = asyncio.run(store.read(MemoryQuery(tenant_id="demo", user_id="alice"), _auth("alice")))
    bob = asyncio.run(store.read(MemoryQuery(tenant_id="demo", user_id="bob"), _auth("bob")))
    other = asyncio.run(
        store.read(MemoryQuery(tenant_id="other", user_id="alice"), _auth("alice", "other"))
    )
    assert [item.content for item in alice] == ["alice 偏好表格"]
    assert [item.content for item in bob] == ["bob 偏好列表"]
    assert [item.content for item in other] == ["其他租户偏好"]


def test_kinds_filter_and_relevance_ranking() -> None:
    store = InMemoryMemory()
    auth = _auth()
    asyncio.run(store.write(_item("semantic 年假说明", MemoryKind.SEMANTIC.value), auth))
    asyncio.run(store.write(_item("episodic 年假处理记录", MemoryKind.EPISODIC.value), auth))

    semantic = asyncio.run(
        store.read(MemoryQuery(tenant_id="demo", user_id="tester", kinds=["semantic"]), auth)
    )
    assert [item.kind for item in semantic] == [MemoryKind.SEMANTIC.value]

    ranked = asyncio.run(
        store.read(
            MemoryQuery(tenant_id="demo", user_id="tester", text="episodic", limit=2), auth
        )
    )
    assert ranked[0].kind == MemoryKind.EPISODIC.value


def test_distill_episode_excludes_evidence_text() -> None:
    state = make_state("一线城市住宿标准是多少？").model_copy(
        update={
            "status": RunStatus.SUCCEEDED,
            "analysis": {"intent": "查事实", "complexity": "simple"},
            "answer": Answer(text="一线城市住宿标准为 800 元/晚。"),
            "evidence": [
                Evidence(
                    id="ev1",
                    source_name="travel_policy",
                    content="EVIDENCE_BODY_SECRET 不应进入记忆",
                )
            ],
        }
    )
    item = distill_episode(state)
    assert item is not None
    assert item.kind == MemoryKind.EPISODIC.value
    assert "800 元" in item.content
    assert "EVIDENCE_BODY_SECRET" not in item.content
    assert item.metadata["status"] == "succeeded"


def test_distill_episode_for_abstained_includes_reason() -> None:
    state = make_state("未收录的问题").model_copy(
        update={
            "status": RunStatus.ABSTAINED,
            "analysis": {"intent": "查事实", "abstain_reason": "insufficient_evidence"},
            "answer": Answer(text="", abstained=True),
        }
    )
    item = distill_episode(state)
    assert item is not None
    assert "insufficient_evidence" in item.content


class _BrokenMemory:
    async def write(self, item, ctx) -> None:
        raise RuntimeError("storage down")


def test_distill_write_failure_does_not_raise() -> None:
    state = make_state("年假天数是多少？").model_copy(
        update={"status": RunStatus.SUCCEEDED, "answer": Answer(text="5 天")}
    )
    assert asyncio.run(distill_and_write(_BrokenMemory(), state, _auth())) is False
