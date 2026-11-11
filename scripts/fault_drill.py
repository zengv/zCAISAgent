"""故障演练入口：执行四类故障场景并输出 JSON 报告。"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from zcaisagent.operations.fault_drill import (  # noqa: E402
    DEFAULT_REPORT_PATH,
    run_drills,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="zCAISAgent 故障演练")
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_REPORT_PATH,
        help="报告输出路径（默认 data/drills/fault-drill.json）",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = asyncio.run(run_drills(args.out))
    for scenario in report["scenarios"]:
        status = "PASS" if scenario["passed"] else "FAIL"
        print(f"[{status}] {scenario['name']}: {scenario['detail']}")
        print(f"       降级路径: {scenario['degraded_path']}")
    print(f"report: {args.out}")
    print(f"overall: {'PASS' if report['passed'] else 'FAIL'}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
