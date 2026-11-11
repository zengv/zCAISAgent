import asyncio
import json
import re

import httpx
import pytest

from zcaisagent.evaluation.loadtest import (
    evaluate_slo,
    load_slo,
    run_load,
    summarize,
    write_report,
)

_SLO_TEXT = """
[default]
p95_ms = 3000
max_error_rate = 0.01

[concurrency.20]
p95_ms = 3200
max_error_rate = 0.01

[concurrency.50]
p95_ms = 6000
max_error_rate = 0.02
"""


def _write_slo(tmp_path, text: str = _SLO_TEXT):
    path = tmp_path / "slo.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_summarize_empty() -> None:
    summary = summarize([], {}, 0.0)
    assert summary["requests"] == 0
    assert summary["rps"] == 0.0
    assert summary["errors"] == 0
    assert summary["error_rate"] == 0.0
    assert summary["latency_ms"] == {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    assert summary["status_counts"] == {}


def test_summarize_single_value_and_rps() -> None:
    summary = summarize([12.5], {"200": 1}, 0.5)
    assert summary["requests"] == 1
    assert summary["rps"] == 2.0
    assert summary["latency_ms"] == {
        "p50": 12.5,
        "p95": 12.5,
        "p99": 12.5,
        "max": 12.5,
    }
    assert summary["errors"] == 0


def test_summarize_mixed_values_errors_and_status_counts() -> None:
    summary = summarize([10, 20, 30, 40], {"200": 3, "500": 1}, 2.0)
    assert summary["requests"] == 4
    assert summary["rps"] == 2.0
    assert summary["latency_ms"] == {"p50": 25.0, "p95": 38.5, "p99": 39.7, "max": 40.0}
    assert summary["errors"] == 1
    assert summary["error_rate"] == 0.25
    assert summary["status_counts"] == {"200": 3, "500": 1}


def test_summarize_counts_exceptions() -> None:
    summary = summarize([5.0, 6.0, 7.0], {"200": 2, "exception": 1}, 1.0)
    assert summary["requests"] == 3
    assert summary["errors"] == 1
    assert summary["error_rate"] == 0.3333


def test_load_slo_defaults_and_concurrency_overrides(tmp_path) -> None:
    slo = load_slo(_write_slo(tmp_path))
    assert slo["default"] == {"p95_ms": 3000.0, "max_error_rate": 0.01}
    assert slo["concurrency"] == {
        20: {"p95_ms": 3200.0, "max_error_rate": 0.01},
        50: {"p95_ms": 6000.0, "max_error_rate": 0.02},
    }


def test_load_slo_only_default(tmp_path) -> None:
    slo = load_slo(_write_slo(tmp_path, "[default]\np95_ms = 100\nmax_error_rate = 0\n"))
    assert slo["concurrency"] == {}


@pytest.mark.parametrize(
    "text",
    [
        "[default\np95_ms = 1",
        "[concurrency.20]\np95_ms = 1\nmax_error_rate = 0.1\n",
        '[default]\np95_ms = "fast"\nmax_error_rate = 0.1\n',
        "[default]\np95_ms = 100\n",
        "[default]\np95_ms = 100\nmax_error_rate = 2\n",
        "[default]\np95_ms = 100\nmax_error_rate = 0.1\n[concurrency.fast]\np95_ms = 1\n",
    ],
)
def test_load_slo_rejects_invalid_file(tmp_path, text: str) -> None:
    with pytest.raises(ValueError):
        load_slo(_write_slo(tmp_path, text))


def test_load_slo_missing_file(tmp_path) -> None:
    with pytest.raises(ValueError):
        load_slo(tmp_path / "missing.toml")


def test_evaluate_slo_passes() -> None:
    summary = {
        "latency_ms": {"p95": 2000.0},
        "error_rate": 0.0,
        "concurrency": 20,
    }
    result = evaluate_slo(summary, {"default": {"p95_ms": 3000, "max_error_rate": 0.01}})
    assert result == {"passed": True, "violations": []}


def test_evaluate_slo_flags_p95_and_error_rate() -> None:
    slo = {"default": {"p95_ms": 3000.0, "max_error_rate": 0.01}}
    summary = {"latency_ms": {"p95": 3500.0}, "error_rate": 0.05, "concurrency": 20}
    result = evaluate_slo(summary, slo)
    assert result["passed"] is False
    assert [item["metric"] for item in result["violations"]] == [
        "p95_ms",
        "max_error_rate",
    ]


def test_evaluate_slo_uses_concurrency_override(tmp_path) -> None:
    slo = load_slo(_write_slo(tmp_path))
    summary = {
        "latency_ms": {"p95": 5000.0},
        "error_rate": 0.015,
        "concurrency": 50,
    }
    assert evaluate_slo(summary, slo)["passed"] is True
    summary["concurrency"] = 20
    result = evaluate_slo(summary, slo)
    assert result["passed"] is False
    assert len(result["violations"]) == 2


def test_run_load_uses_worker_pool_and_records_latency() -> None:
    in_flight = 0
    max_in_flight = 0
    payloads: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        payloads.append(json.loads(request.content))
        await asyncio.sleep(0.01)
        in_flight -= 1
        return httpx.Response(
            200, json={"run_id": "run_1", "status": "succeeded", "answer": "secret"}
        )

    report = asyncio.run(
        run_load(
            "http://test",
            "secret-key",
            concurrency=5,
            requests=13,
            transport=httpx.MockTransport(handler),
        )
    )

    assert report["summary"]["requests"] == 13
    assert report["summary"]["errors"] == 0
    assert report["summary"]["error_rate"] == 0.0
    assert report["summary"]["status_counts"] == {"200": 13}
    assert report["summary"]["latency_ms"]["max"] > 0
    assert report["concurrency"] == 5
    assert report["run_status_counts"] == {"succeeded": 13}
    assert 1 < max_in_flight <= 5
    assert payloads == [{"goal": "一线城市住宿标准是多少？", "level": "L1"}] * 13
    assert "secret-key" not in json.dumps(report)


def test_run_load_counts_http_and_transport_errors() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls % 4 == 0:
            raise httpx.ConnectError("boom", request=request)
        if calls % 3 == 0:
            return httpx.Response(500, json={"status": "failed"})
        return httpx.Response(200, json={"status": "succeeded"})

    report = asyncio.run(
        run_load(
            "http://test",
            "dev-key",
            concurrency=4,
            requests=12,
            transport=httpx.MockTransport(handler),
        )
    )

    summary = report["summary"]
    assert summary["requests"] == 12
    assert summary["errors"] == 6
    assert summary["error_rate"] == 0.5
    assert summary["status_counts"] == {"200": 6, "500": 3, "exception": 3}
    assert report["run_status_counts"] == {"succeeded": 6, "failed": 3}


def test_run_load_rejects_invalid_arguments() -> None:
    with pytest.raises(ValueError):
        asyncio.run(run_load("http://test", "k", concurrency=0))
    with pytest.raises(ValueError):
        asyncio.run(run_load("http://test", "k", requests=-1))


def test_write_report_is_sanitized_and_named(tmp_path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"run_id": "run_1", "status": "succeeded", "answer": "TOP-SECRET-ANSWER"},
        )

    report = asyncio.run(
        run_load(
            "http://test",
            "super-secret-key",
            concurrency=3,
            requests=2,
            transport=httpx.MockTransport(handler),
        )
    )
    path = write_report(report, tmp_path / "perf")

    assert path.parent == tmp_path / "perf"
    assert re.fullmatch(r"loadtest-3-\d{8}T\d{6}Z\.json", path.name)
    text = path.read_text(encoding="utf-8")
    assert "super-secret-key" not in text
    assert "TOP-SECRET-ANSWER" not in text
    assert "Authorization" not in text
    payload = json.loads(text)
    assert payload["summary"]["requests"] == 2
    assert payload["concurrency"] == 3
