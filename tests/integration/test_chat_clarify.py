import json
from uuid import uuid4

from fastapi.testclient import TestClient

from zcaisagent.api.app import app

client = TestClient(app)


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in text.strip().split("\n\n"):
        name = None
        payload = None
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                payload = json.loads(line[5:].strip())
        if name:
            events.append((name, payload or {}))
    return events


def test_clarify_payload() -> None:
    # 唯一 thread，避免与既有磁盘 checkpoint 相互干扰。
    thread_id = f"thread_clarify_payload_{uuid4().hex}"
    headers = {"X-API-Key": "dev-key"}

    # 第一轮：并列主体，写入历史，供第二轮指代候选。
    first = client.post(
        "/v1/chat",
        json={"goal": "年假和病假分别几天？", "thread_id": thread_id},
        headers=headers,
    )
    assert first.status_code == 200
    assert _parse_sse(first.text)[-1][0] == "done"

    # 第二轮：指代不清，应触发澄清并停在 awaiting_user。
    second = client.post(
        "/v1/chat",
        json={"goal": "它呢？", "thread_id": thread_id},
        headers=headers,
    )
    assert second.status_code == 200
    events = _parse_sse(second.text)
    assert events[-1][0] == "done"

    done = events[-1][1]
    assert done["status"] == "awaiting_user"
    assert done["stage"] == "person/answer"
    assert done["clarify"] is not None
    questions = done["clarify"]["questions"]
    assert len(questions) >= 1
    assert 2 <= len(questions[0]["options"]) <= 4

    # 明确问句（新 thread）不应产生澄清。
    clear = client.post(
        "/v1/chat",
        json={
            "goal": "出差住宿标准是多少？",
            "thread_id": f"thread_clarify_clear_{uuid4().hex}",
        },
        headers=headers,
    )
    assert clear.status_code == 200
    clear_done = _parse_sse(clear.text)[-1]
    assert clear_done[0] == "done"
    assert clear_done[1]["clarify"] is None


def test_clarify_then_answer_in_same_thread() -> None:
    # 唯一 thread：澄清后继续在同一会话作答，且 thread_id 全程不变。
    thread_id = f"thread_clarify_answer_{uuid4().hex}"
    headers = {"X-API-Key": "dev-key"}

    def chat(goal: str) -> list[tuple[str, dict]]:
        response = client.post(
            "/v1/chat",
            json={"goal": goal, "thread_id": thread_id},
            headers=headers,
        )
        assert response.status_code == 200
        events = _parse_sse(response.text)
        assert events[-1][0] == "done"
        return events

    first_events = chat("年假和病假分别几天？")
    second_events = chat("它呢？")
    second_done = second_events[-1][1]
    assert second_done["status"] == "awaiting_user"
    assert second_done["clarify"] is not None

    # 第三轮选择澄清选项之一，应恢复正常作答而不是再次追问。
    option = second_done["clarify"]["questions"][0]["options"][0]
    third_events = chat(option)
    third_done = third_events[-1][1]
    assert third_done["status"] != "awaiting_user"
    assert third_done["clarify"] is None
    assert third_done["answer"].strip()

    thread_ids = {
        events[0][1]["thread_id"]
        for events in (first_events, second_events, third_events)
    }
    assert thread_ids == {thread_id}


def test_clarify_compat_fields() -> None:
    # 澄清响应的 done 仍保留既有字段，确保前端向后兼容。
    thread_id = f"thread_clarify_compat_{uuid4().hex}"
    headers = {"X-API-Key": "dev-key"}
    client.post(
        "/v1/chat",
        json={"goal": "年假和病假分别几天？", "thread_id": thread_id},
        headers=headers,
    )
    response = client.post(
        "/v1/chat",
        json={"goal": "它呢？", "thread_id": thread_id},
        headers=headers,
    )
    assert response.status_code == 200
    done = _parse_sse(response.text)[-1][1]
    assert done["status"] == "awaiting_user"
    assert done["clarify"] is not None

    for field in (
        "run_id",
        "status",
        "answer",
        "abstained",
        "citations",
        "confidence",
        "limitations",
        "metrics",
    ):
        assert field in done
    assert done["run_id"]
    assert done["answer"]
    assert isinstance(done["citations"], list)
    assert isinstance(done["limitations"], list)
    assert isinstance(done["metrics"], dict)
