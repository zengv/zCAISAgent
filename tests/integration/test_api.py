from uuid import uuid4

from fastapi.testclient import TestClient

from zcaisagent.api.app import app

client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_create_run_with_citation() -> None:
    response = client.post(
        "/v1/runs",
        json={"goal": "一线城市住宿标准是多少？"},
        headers={"X-API-Key": "dev-key"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["evidence_count"] >= 1
    assert body["citations"]


def test_chat_stream() -> None:
    response = client.post(
        "/v1/chat",
        json={"goal": "婚假有几天？"},
        headers={"X-API-Key": "dev-key"},
    )
    assert response.status_code == 200
    assert "event: meta" in response.text
    assert "event: done" in response.text


def test_unauthorized() -> None:
    response = client.post(
        "/v1/runs",
        json={"goal": "你好"},
        headers={"X-API-Key": "wrong-key"},
    )
    assert response.status_code == 401


def test_blank_goal_is_rejected() -> None:
    response = client.post(
        "/v1/runs",
        json={"goal": "   "},
        headers={"X-API-Key": "dev-key"},
    )
    assert response.status_code == 400


def test_thread_reuse_carries_history() -> None:
    thread_id = f"thread_{uuid4().hex}"
    headers = {"X-API-Key": "dev-key"}
    first = client.post(
        "/v1/runs",
        json={"goal": "年假天数是多少？", "thread_id": thread_id},
        headers=headers,
    )
    assert first.status_code == 200
    second = client.post(
        "/v1/runs",
        json={"goal": "它呢？", "thread_id": thread_id},
        headers=headers,
    )
    assert second.status_code == 200
    assert second.json()["thread_id"] == thread_id


def test_cross_user_thread_is_forbidden() -> None:
    thread_id = f"thread_{uuid4().hex}"
    headers = {"X-API-Key": "dev-key"}
    first = client.post(
        "/v1/runs",
        json={"goal": "年假天数是多少？", "thread_id": thread_id, "user_id": "alice"},
        headers=headers,
    )
    assert first.status_code == 200
    second = client.post(
        "/v1/runs",
        json={"goal": "它呢？", "thread_id": thread_id, "user_id": "bob"},
        headers=headers,
    )
    assert second.status_code == 403
