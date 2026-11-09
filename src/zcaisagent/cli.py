import argparse
import asyncio
import json
from pathlib import Path

from zcaisagent.container import build_container, build_context, create_state
from zcaisagent.domain.enums import AutonomyLevel
from zcaisagent.domain.models import RunState
from zcaisagent.orchestration.build import build_default_graph
from zcaisagent.orchestration.runtime import Runtime


async def _run(container, query: str, level: AutonomyLevel) -> RunState:
    auth = await container.identity.authenticate(
        container.settings.api_key, {"tenant_id": container.settings.default_tenant}
    )
    state = create_state(container, goal=query, level=level, auth=auth)
    ctx = build_context(container, auth)
    return await Runtime().execute(build_default_graph(), state, ctx)


def _print_state(state: RunState) -> None:
    answer = state.answer
    print(f"run_id: {state.run_id}")
    print(f"status: {state.status.value}  steps: {state.step}")
    print(f"answer: {answer.text if answer else ''}")
    print(f"confidence: {answer.confidence if answer else 'low'}")
    if answer:
        for citation in answer.citations:
            print(f"  [{citation.evidence_id}] confidence={citation.confidence}")
        for limitation in answer.limitations:
            print(f"  limitation: {limitation}")
    print(f"trace: {container_path(state)}")


def container_path(state: RunState) -> str:
    return f".zcaisagent/trace.jsonl ({state.run_id})"


def cmd_ask(args: argparse.Namespace) -> int:
    container = build_container()
    level = _parse_level(args.level)
    state = asyncio.run(_run(container, args.query, level))
    _print_state(state)
    return 0 if state.status.value == "succeeded" else 1


def cmd_eval(args: argparse.Namespace) -> int:
    from zcaisagent.evaluation.runner import filter_cases, load_cases, run_dataset

    cases = load_cases(Path(args.path))
    cases = filter_cases(cases, tag=args.tag, level=args.level)
    container = build_container()
    report = asyncio.run(run_dataset(container, cases))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_grade(args: argparse.Namespace) -> int:
    from zcaisagent.evaluation.grading import load_grading_cases, run_grading

    cases = load_grading_cases(Path(args.path))
    container = build_container()
    report = asyncio.run(run_grading(container, cases))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["pass"] else 1


def _parse_level(value: str | None) -> AutonomyLevel:
    if not value:
        return AutonomyLevel.L1_FAST
    try:
        return AutonomyLevel(value)
    except ValueError:
        return AutonomyLevel.L1_FAST


def main() -> int:
    parser = argparse.ArgumentParser(prog="zcaisagent")
    sub = parser.add_subparsers(dest="command", required=True)

    ask = sub.add_parser("ask", help="ask a single question")
    ask.add_argument("query")
    ask.add_argument("--level", default="L1", choices=["L0", "L1", "L2", "L3"])
    ask.set_defaults(func=cmd_ask)

    evaluate = sub.add_parser("eval", help="run golden set")
    evaluate.add_argument("path", nargs="?", default="data/eval/golden.jsonl")
    evaluate.add_argument("--tag", default=None, help="only run cases with this tag")
    evaluate.add_argument(
        "--level", default=None, choices=["L0", "L1", "L2", "L3"],
        help="only run cases at this level",
    )
    evaluate.set_defaults(func=cmd_eval)

    grading = sub.add_parser("grade", help="run routing grading set")
    grading.add_argument("path", nargs="?", default="data/eval/grading.jsonl")
    grading.set_defaults(func=cmd_grade)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
