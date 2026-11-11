"""压测与容量规划：固定请求数的异步并发、分位统计与 SLO 判定。"""

from __future__ import annotations

import asyncio
import json
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from zcaisagent.evaluation.observability import percentile

_DEFAULT_GOAL = "一线城市住宿标准是多少？"
_DEFAULT_LEVEL = "L1"


def _parse_thresholds(section: Any, label: str) -> dict[str, float]:
    """校验并归一化单档阈值；缺字段或类型错误抛 ValueError。"""
    if not isinstance(section, dict):
        raise ValueError(f"SLO 缺少 {label} 阈值")
    p95_ms = section.get("p95_ms")
    max_error_rate = section.get("max_error_rate")
    if isinstance(p95_ms, bool) or not isinstance(p95_ms, (int, float)):
        raise ValueError(f"SLO {label}.p95_ms 必须是数字")
    if isinstance(max_error_rate, bool) or not isinstance(max_error_rate, (int, float)):
        raise ValueError(f"SLO {label}.max_error_rate 必须是数字")
    if p95_ms <= 0:
        raise ValueError(f"SLO {label}.p95_ms 必须大于 0")
    if not 0 <= max_error_rate <= 1:
        raise ValueError(f"SLO {label}.max_error_rate 必须在 [0, 1] 内")
    return {"p95_ms": float(p95_ms), "max_error_rate": float(max_error_rate)}


def load_slo(path: Path) -> dict[str, Any]:
    """读取 SLO TOML：``[default]`` 必填，``[concurrency.N]`` 按并发档位覆盖。"""
    try:
        with Path(path).open("rb") as fh:
            raw = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"无法读取 SLO 配置 {path}: {exc}") from exc

    default = _parse_thresholds(raw.get("default"), "default")
    overrides: dict[int, dict[str, float]] = {}
    for key, section in (raw.get("concurrency", {}) or {}).items():
        try:
            level = int(key)
        except (TypeError, ValueError):
            raise ValueError(f"SLO 并发档位必须是整数: {key}") from None
        if level <= 0:
            raise ValueError(f"SLO 并发档位必须大于 0: {key}")
        overrides[level] = _parse_thresholds(section, f"concurrency.{level}")
    return {"default": default, "concurrency": overrides}


def _thresholds_for(slo: dict[str, Any], concurrency: Any) -> dict[str, float]:
    default = slo.get("default")
    if not isinstance(default, dict):
        raise ValueError("SLO 缺少 default 阈值")
    thresholds = dict(default)
    overrides = slo.get("concurrency") or {}
    try:
        level = int(concurrency)
    except (TypeError, ValueError):
        level = None
    override = overrides.get(level) if level is not None else None
    if isinstance(override, dict):
        thresholds.update(override)
    return thresholds


def _is_error_status(status: str) -> bool:
    if status == "exception":
        return True
    try:
        return int(status) >= 400
    except ValueError:
        return False


def summarize(
    latencies_ms: list[float],
    status_counts: dict[str, int],
    duration_s: float,
) -> dict[str, Any]:
    """汇总压测结果：吞吐、延迟分位、错误数与错误率。"""
    counts = {str(key): int(value) for key, value in (status_counts or {}).items()}
    requests = sum(counts.values()) if counts else len(latencies_ms)
    errors = sum(count for status, count in counts.items() if _is_error_status(status))
    return {
        "requests": requests,
        "rps": round(requests / duration_s, 2) if duration_s > 0 else 0.0,
        "latency_ms": {
            "p50": round(percentile(latencies_ms, 50), 2),
            "p95": round(percentile(latencies_ms, 95), 2),
            "p99": round(percentile(latencies_ms, 99), 2),
            "max": round(max(latencies_ms), 2) if latencies_ms else 0.0,
        },
        "errors": errors,
        "error_rate": round(errors / requests, 4) if requests else 0.0,
        "status_counts": counts,
    }


def evaluate_slo(summary: dict[str, Any], slo: dict[str, Any]) -> dict[str, Any]:
    """按 summary 的并发档位取阈值，检查 P95 与错误率。"""
    thresholds = _thresholds_for(slo, summary.get("concurrency"))
    p95 = float((summary.get("latency_ms") or {}).get("p95", 0.0))
    error_rate = float(summary.get("error_rate", 0.0))
    violations: list[dict[str, Any]] = []
    if p95 > thresholds["p95_ms"]:
        violations.append(
            {
                "metric": "p95_ms",
                "actual": round(p95, 2),
                "threshold": thresholds["p95_ms"],
            }
        )
    if error_rate > thresholds["max_error_rate"]:
        violations.append(
            {
                "metric": "max_error_rate",
                "actual": round(error_rate, 4),
                "threshold": thresholds["max_error_rate"],
            }
        )
    return {"passed": not violations, "violations": violations}


async def _request_once(
    client: httpx.AsyncClient,
    path: str,
    goal: str,
    level: str,
) -> tuple[float, str, str | None]:
    """发起一次请求，返回 (延迟 ms, HTTP 状态, body.status)；不保留正文。"""
    started = time.perf_counter()
    try:
        response = await client.post(path, json={"goal": goal, "level": level})
    except Exception:  # noqa: BLE001 - 网络/超时/解析异常统一计入错误率
        return round((time.perf_counter() - started) * 1000, 2), "exception", None
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    try:
        body = response.json()
    except ValueError:
        body = None
    run_status = body.get("status") if isinstance(body, dict) else None
    return latency_ms, str(response.status_code), run_status


async def run_load(
    base_url: str,
    api_key: str,
    *,
    concurrency: int = 20,
    requests: int = 100,
    path: str = "/v1/runs",
    goal: str = _DEFAULT_GOAL,
    level: str = _DEFAULT_LEVEL,
    timeout_s: float = 60.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """以固定总数请求压测 API，返回脱敏报告（不含密钥与回答正文）。"""
    if concurrency < 1:
        raise ValueError("concurrency 必须 >= 1")
    if requests < 0:
        raise ValueError("requests 必须 >= 0")

    total = int(requests)
    started = time.perf_counter()
    async with httpx.AsyncClient(
        base_url=base_url,
        headers={"X-API-Key": api_key},
        timeout=timeout_s,
        transport=transport,
    ) as client:
        slots = iter(range(total))

        async def worker() -> list[tuple[float, str, str | None]]:
            results: list[tuple[float, str, str | None]] = []
            while True:
                try:
                    next(slots)
                except StopIteration:
                    return results
                results.append(await _request_once(client, path, goal, level))

        worker_count = min(concurrency, total) if total else 0
        batches = await asyncio.gather(*(worker() for _ in range(worker_count)))

    latencies: list[float] = []
    status_counts: dict[str, int] = {}
    run_status_counts: dict[str, int] = {}
    for batch in batches:
        for latency_ms, status, run_status in batch:
            latencies.append(latency_ms)
            status_counts[status] = status_counts.get(status, 0) + 1
            if run_status:
                run_status_counts[run_status] = run_status_counts.get(run_status, 0) + 1

    duration_s = round(time.perf_counter() - started, 4)
    return {
        "summary": summarize(latencies, status_counts, duration_s),
        "base_url": base_url,
        "concurrency": concurrency,
        "duration_s": duration_s,
        "run_status_counts": run_status_counts,
        "generated_at": datetime.now(UTC).isoformat(),
    }


def write_report(report: dict[str, Any], out_dir: Path) -> Path:
    """把报告写入 out_dir，文件名带并发档位与 UTC 时间戳。"""
    target_dir = Path(out_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = target_dir / f"loadtest-{report.get('concurrency', 0)}-{stamp}.json"
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target
