import json

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


def test_chat_streams_meta_tokens_done() -> None:
    response = client.post(
        "/v1/chat",
        json={"goal": "婚假有几天？"},
        headers={"X-API-Key": "dev-key"},
    )
    assert response.status_code == 200

    events = _parse_sse(response.text)
    names = [name for name, _ in events]
    assert names[0] == "meta"
    # L1 走非流式一次性 JSON 合成，不再逐个 token 推送；正文由 done 事件给出。
    assert names[-1] == "done"

    done = events[-1][1]
    assert done["answer"]
    assert done["run_id"] == events[0][1]["run_id"]
    assert "abstained" in done
