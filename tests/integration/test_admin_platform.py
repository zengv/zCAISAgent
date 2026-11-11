import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zcaisagent.api.app import app
from zcaisagent.domain.enums import RunStatus
from zcaisagent.domain.events import RunEvent
from zcaisagent.domain.models import Answer, Budget, RunState
from zcaisagent.governance.identity import ApiKeyIdentity
from zcaisagent.settings import SecurityConfig, StudioConfig

client = TestClient(app)

_GATE_TOML = (
    "correct_rate = 0.9\n"
    "citation_support = 0.85\n"
    "faithfulness = 0.8\n"
    "context_recall = 0.85\n"
    "abstain_accuracy = 0.9\n"
    "allow_failed = 0\n"
)


def _use_studio(container, monkeypatch, tmp_path) -> StudioConfig:
    """把看板数据路径指向临时目录，避免测试写入仓库。"""
    gate_path = tmp_path / "gate.toml"
    gate_path.write_text(_GATE_TOML, encoding="utf-8")
    studio = StudioConfig(
        eval_report_path=tmp_path / "latest.json",
        eval_gate_path=gate_path,
        baseline_dir=tmp_path / "baselines",
    )
    monkeypatch.setattr(
        container, "settings", replace(container.settings, studio=studio)
    )
    return studio


def _identity(container, tenant="demo", roles=None, security=None):
    return ApiKeyIdentity(container.settings.api_key, tenant, security, roles=roles)


def _save_run(
    container,
    run_id: str,
    *,
    tenant: str = "demo",
    status: RunStatus = RunStatus.SUCCEEDED,
    created_ts: float | None = None,
    finished_ts: float = 0.0,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost: float = 0.0,
) -> RunState:
    state = RunState(
        run_id=run_id,
        thread_id=f"thread_{run_id}",
        tenant_id=tenant,
        user_id="dev",
        goal="住宿标准是多少？",
        status=status,
        created_ts=time.time() if created_ts is None else created_ts,
        finished_ts=finished_ts,
        budget=Budget(
            used_tokens=tokens_in + tokens_out,
            used_tokens_in=tokens_in,
            used_tokens_out=tokens_out,
            used_cost_usd=cost,
        ),
        answer=Answer(text="答案", abstained=status == RunStatus.ABSTAINED),
    )
    asyncio.run(container.checkpoint.save(state))
    return state


def _denied(container) -> list:
    return [event for event in container.audit.list() if event.action == "admin.denied"]


def test_admin_requires_valid_api_key(offline_api_container) -> None:
    response = client.get("/v1/admin/metrics", headers={"X-API-Key": "wrong-key"})
    assert response.status_code == 401

    denied = _denied(offline_api_container)
    assert any(
        event.actor == "anonymous"
        and event.detail.get("reason") == "unauthorized"
        and event.detail.get("action") == "admin.read"
        for event in denied
    )


def test_admin_requires_scope_for_read_and_write(
    offline_api_container, monkeypatch
) -> None:
    identity = ApiKeyIdentity(
        offline_api_container.settings.api_key,
        "demo",
        SecurityConfig(roles={"support": ["retrieve"]}),
        roles=["support"],
    )
    monkeypatch.setattr(offline_api_container, "identity", identity)

    assert client.get("/v1/admin/sources").status_code == 403
    assert client.post("/v1/admin/eval/run", json={"limit": 1}).status_code == 403

    denied = _denied(offline_api_container)
    assert any(
        event.detail.get("action") == "admin.read"
        and event.detail.get("reason") == "insufficient_scope"
        for event in denied
    )
    assert any(
        event.detail.get("action") == "admin.write"
        and event.detail.get("reason") == "insufficient_scope"
        for event in denied
    )


def test_sources_list_and_toggle(offline_api_container) -> None:
    listed = client.get("/v1/admin/sources")
    assert listed.status_code == 200
    sources = listed.json()
    names = {item["name"]: item for item in sources}
    assert {"inmemory", "bm25"} <= set(names)
    assert all(item["health"] for item in sources)
    assert isinstance(names["inmemory"]["type"], str)
    assert isinstance(names["inmemory"]["priority"], int)

    original = names["bm25"]["enabled"]
    toggled = client.put("/v1/admin/sources/bm25", json={"enabled": not original})
    assert toggled.status_code == 200
    assert toggled.json()["enabled"] is (not original)

    restored = client.put("/v1/admin/sources/bm25", json={"enabled": original})
    assert restored.status_code == 200
    assert restored.json()["enabled"] is original

    assert client.put(
        "/v1/admin/sources/missing-source", json={"enabled": True}
    ).status_code == 404
    assert any(
        event.action == "admin.source_toggled"
        for event in offline_api_container.audit.list()
    )


def test_runs_list_order_filter_and_tenant(offline_api_container, monkeypatch) -> None:
    tenant = "tenant-runs"
    monkeypatch.setattr(
        offline_api_container, "identity", _identity(offline_api_container, tenant)
    )
    now = time.time()
    _save_run(
        offline_api_container, "run_admin_a", tenant=tenant,
        status=RunStatus.SUCCEEDED, created_ts=now - 30, finished_ts=now - 28,
        tokens_in=100, cost=0.001,
    )
    _save_run(
        offline_api_container, "run_admin_b", tenant=tenant,
        status=RunStatus.ABSTAINED, created_ts=now - 10, finished_ts=now - 9,
    )
    _save_run(
        offline_api_container, "run_admin_c", tenant=tenant,
        status=RunStatus.SUCCEEDED, created_ts=now - 20, finished_ts=now - 19,
    )
    _save_run(
        offline_api_container, "run_admin_other", tenant="tenant-other",
        status=RunStatus.SUCCEEDED, created_ts=now,
    )

    response = client.get("/v1/admin/runs", params={"limit": 10})
    assert response.status_code == 200
    runs = response.json()
    assert [item["run_id"] for item in runs] == [
        "run_admin_b",
        "run_admin_c",
        "run_admin_a",
    ]
    assert "run_admin_other" not in {item["run_id"] for item in runs}
    first = runs[0]
    for key in (
        "thread_id", "tenant_id", "user_id", "status", "stage", "created_ts",
        "finished_ts", "steps", "tokens", "cost", "answer_abstained",
    ):
        assert key in first
    assert first["answer_abstained"] is True

    filtered = client.get("/v1/admin/runs", params={"status": "abstained"})
    assert [item["run_id"] for item in filtered.json()] == ["run_admin_b"]


def test_trace_same_tenant_cross_tenant_and_missing(
    offline_api_container, monkeypatch
) -> None:
    tenant = "tenant-trace"
    monkeypatch.setattr(
        offline_api_container, "identity", _identity(offline_api_container, tenant)
    )
    _save_run(offline_api_container, "run_admin_trace", tenant=tenant)
    offline_api_container.trace.event(
        RunEvent(type="step.finished", run_id="run_admin_trace", node="retrieve")
    )
    _save_run(offline_api_container, "run_admin_foreign", tenant="tenant-other")

    allowed = client.get("/v1/admin/runs/run_admin_trace/trace")
    assert allowed.status_code == 200
    body = allowed.json()
    assert body["run"]["run_id"] == "run_admin_trace"
    assert body["events"]

    denied = client.get("/v1/admin/runs/run_admin_foreign/trace")
    assert denied.status_code == 403
    assert any(
        event.detail.get("reason") == "cross_tenant_denied"
        for event in _denied(offline_api_container)
    )

    assert client.get("/v1/admin/runs/run_absent/trace").status_code == 404


def test_metrics_empty_window(offline_api_container, monkeypatch) -> None:
    monkeypatch.setattr(
        offline_api_container,
        "identity",
        _identity(offline_api_container, "tenant-metrics-empty"),
    )
    response = client.get("/v1/admin/metrics", params={"window": "1h"})
    assert response.status_code == 200
    body = response.json()
    assert body["empty"] is True
    assert body["requests"] == 0
    assert body["success_rate"] == 0.0
    assert body["latency_ms"] == {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    assert body["tokens"] == {"total": 0, "in": 0, "out": 0}


def test_metrics_aggregates_runs(offline_api_container, monkeypatch) -> None:
    tenant = "tenant-metrics"
    monkeypatch.setattr(
        offline_api_container, "identity", _identity(offline_api_container, tenant)
    )
    now = time.time()
    _save_run(
        offline_api_container, "run_metric_ok", tenant=tenant,
        status=RunStatus.SUCCEEDED, created_ts=now - 2, finished_ts=now,
        tokens_in=100, tokens_out=50, cost=0.002,
    )
    _save_run(
        offline_api_container, "run_metric_abstain", tenant=tenant,
        status=RunStatus.ABSTAINED, created_ts=now - 1,
    )

    response = client.get("/v1/admin/metrics", params={"window": "24h"})
    assert response.status_code == 200
    body = response.json()
    assert body["empty"] is False
    assert body["requests"] == 2
    assert body["success_rate"] == 0.5
    assert body["abstain_rate"] == 0.5
    assert body["latency_ms"]["p95"] == pytest.approx(2000.0, abs=1.0)
    assert body["tokens"] == {"total": 150, "in": 100, "out": 50}
    assert body["cost_usd"] == pytest.approx(0.002)
    assert body["cache"]["enabled"] is False
    assert "quality" in body


def test_metrics_rejects_invalid_window(offline_api_container) -> None:
    response = client.get("/v1/admin/metrics", params={"window": "24x"})
    assert response.status_code == 400


def test_config_snapshot_has_no_secrets(offline_api_container) -> None:
    response = client.get("/v1/admin/config")
    assert response.status_code == 200
    payload = response.json()
    text = json.dumps(payload, ensure_ascii=False)

    # identity_mode 的取值 "api_key" 是模式名而非密钥；这里只禁止密钥字段与值。
    assert '"api_key":' not in text
    assert '"dsn":' not in text
    assert "AI_KEY" not in text
    assert "postgresql://" not in text
    assert offline_api_container.settings.api_key not in text
    for key in (
        "models", "purpose_effort", "retrieval", "security", "tools",
        "memory", "cache", "scheduler", "events", "database",
    ):
        assert key in payload


def test_eval_latest_missing_is_404(offline_api_container, monkeypatch, tmp_path) -> None:
    _use_studio(offline_api_container, monkeypatch, tmp_path)
    assert client.get("/v1/admin/eval/latest").status_code == 404


def test_eval_latest_merges_gate(offline_api_container, monkeypatch, tmp_path) -> None:
    report = {
        "total": 2,
        "failed": 0,
        "correct_rate": 0.95,
        "abstain_accuracy": 0.95,
        "avg_citation_support": 0.9,
        "avg_faithfulness": 0.9,
        "avg_context_recall": 0.9,
        "generated_at": "2026-01-01T00:00:00+00:00",
    }
    studio = _use_studio(offline_api_container, monkeypatch, tmp_path)
    report_path = studio.eval_report_path
    report_path.write_text(json.dumps(report), encoding="utf-8")

    response = client.get("/v1/admin/eval/latest")
    assert response.status_code == 200
    body = response.json()
    assert body["report"]["total"] == 2
    assert body["gate"] == {"passed": True, "violations": []}
    assert body["thresholds"]["correct_rate"] == 0.9
    assert body["path"] == str(report_path)
    assert body["generated_at"] == "2026-01-01T00:00:00+00:00"


def test_metrics_quality_reads_studio_report(
    offline_api_container, monkeypatch, tmp_path
) -> None:
    report = {
        "total": 1,
        "failed": 0,
        "correct_rate": 1.0,
        "abstain_accuracy": 1.0,
        "avg_citation_support": 1.0,
        "avg_faithfulness": 1.0,
        "avg_context_recall": 1.0,
        "generated_at": "2026-01-01T00:00:00+00:00",
    }
    studio = _use_studio(offline_api_container, monkeypatch, tmp_path)
    studio.eval_report_path.write_text(json.dumps(report), encoding="utf-8")

    metrics = client.get("/v1/admin/metrics", params={"window": "24h"}).json()
    assert metrics["quality"]["available"] is True
    assert metrics["quality"]["path"] == str(studio.eval_report_path)
    assert metrics["quality"]["report"]["total"] == 1

    latest = client.get("/v1/admin/eval/latest")
    assert latest.status_code == 200
    assert latest.json()["report"]["total"] == 1


def test_eval_run_returns_report_and_writes_latest(
    offline_api_container, monkeypatch, tmp_path
) -> None:
    studio = _use_studio(offline_api_container, monkeypatch, tmp_path)

    response = client.post("/v1/admin/eval/run", json={"limit": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["report"]["total"] == 1
    assert body["report"]["cases"] == 1
    assert "thresholds" in body
    assert body["gate"]["passed"] in {True, False}
    assert body["generated_at"] == body["report"]["generated_at"]
    assert Path(body["path"]).is_file()
    assert body["report_path"] == str(studio.eval_report_path)

    saved = json.loads(studio.eval_report_path.read_text(encoding="utf-8"))
    assert saved["cases"] == 1
    assert saved["generated_at"] == body["generated_at"]

    metrics = client.get("/v1/admin/metrics", params={"window": "24h"}).json()
    assert metrics["quality"]["available"] is True
    assert metrics["quality"]["report"]["total"] == 1
