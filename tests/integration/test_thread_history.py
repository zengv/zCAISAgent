from uuid import uuid4

from fastapi.testclient import TestClient

from zcaisagent.api.app import app

client = TestClient(app)
_HEADERS = {"X-API-Key": "dev-key"}


def _chat(goal: str, thread_id: str, user_id: str = "web"):
    return client.post(
        "/v1/chat",
        json={"goal": goal, "thread_id": thread_id, "user_id": user_id},
        headers=_HEADERS,
    )


def _history(thread_id: str, user_id: str = "web", **params):
    return client.get(
        f"/v1/threads/{thread_id}/messages",
        params={
            "tenant_id": "demo",
            "user_id": user_id,
            "limit": 20,
            **params,
        },
        headers=_HEADERS,
    )


def test_thread_history_replays_rounds() -> None:
    thread_id = f"thread_history_{uuid4().hex}"
    assert _chat("年假和病假分别几天？", thread_id).status_code == 200
    assert _chat("它呢？", thread_id).status_code == 200

    response = _history(thread_id)
    assert response.status_code == 200
    data = response.json()
    assert data["thread_id"] == thread_id
    assert len(data["messages"]) == 2

    first, second = data["messages"]
    assert first["goal"] == "年假和病假分别几天？"
    assert first["answer"].strip()
    assert first["status"] != "awaiting_user"
    assert isinstance(first["citations"], list)

    assert second["status"] == "awaiting_user"
    assert second["stage"] == "person/answer"
    assert second["clarify"] is not None
    assert 2 <= len(second["clarify"]["questions"][0]["options"]) <= 4


def test_thread_history_unknown_returns_empty() -> None:
    response = _history(f"thread_missing_{uuid4().hex}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["messages"] == []
    assert payload["has_more"] is False


def test_thread_history_cross_user_denied() -> None:
    thread_id = f"thread_history_denied_{uuid4().hex}"
    assert _chat("年假和病假分别几天？", thread_id, user_id="web").status_code == 200

    response = _history(thread_id, user_id="other")
    assert response.status_code == 403
    assert response.json()["detail"] == "thread not accessible"


def test_thread_history_limit_and_before() -> None:
    thread_id = f"thread_history_page_{uuid4().hex}"
    assert _chat("年假和病假分别几天？", thread_id).status_code == 200
    assert _chat("它呢？", thread_id).status_code == 200

    response = _history(thread_id, limit=1)
    assert response.status_code == 200
    data = response.json()
    assert len(data["messages"]) == 1
    assert data["has_more"] is True

    # 用窗口首条时间向前翻页，应拿到更早一轮。
    earlier = _history(thread_id, limit=1, before=data["messages"][0]["created_ts"])
    assert earlier.status_code == 200
    assert earlier.json()["messages"][0]["goal"] == "年假和病假分别几天？"
