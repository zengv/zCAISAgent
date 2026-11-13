"""压测入口：对已启动的 API 发起并发请求并按 SLO 判定退出码。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from zcaisagent.evaluation.loadtest import (  # noqa: E402 - 需先注入 src
    evaluate_slo,
    load_slo,
    run_load,
    write_report,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="zCAISAgent 并发压测与 SLO 判定")
    parser.add_argument("--base-url", default="http://127.0.0.1:6278")
    parser.add_argument("--api-key", default=os.getenv("ZCAISAGENT_API_KEY", "dev-key"))
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--path", default="/v1/runs")
    parser.add_argument("--goal", default="一线城市住宿标准是多少？")
    parser.add_argument("--level", default="L1")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--slo", default="configs/slo.toml")
    parser.add_argument("--out-dir", default="data/perf")
    parser.add_argument("--no-slo", action="store_true", help="跳过 SLO 判定")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = asyncio.run(
            run_load(
                args.base_url,
                args.api_key,
                concurrency=args.concurrency,
                requests=args.requests,
                path=args.path,
                goal=args.goal,
                level=args.level,
                timeout_s=args.timeout,
            )
        )
    except ValueError as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        return 2

    report_path = write_report(report, Path(args.out_dir))
    payload: dict[str, object] = {"report_path": str(report_path), **report}
    exit_code = 0
    if not args.no_slo:
        try:
            slo = load_slo(Path(args.slo))
        except ValueError as exc:
            print(f"SLO 配置错误: {exc}", file=sys.stderr)
            return 2
        summary = {**report["summary"], "concurrency": report["concurrency"]}
        result = evaluate_slo(summary, slo)
        payload["slo"] = {"path": args.slo, **result}
        exit_code = 0 if result["passed"] else 1

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
