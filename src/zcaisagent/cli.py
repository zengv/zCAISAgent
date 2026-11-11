import argparse
import asyncio
import json
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from zcaisagent.capabilities.memory.distill import distill_and_write
from zcaisagent.capabilities.memory.policies import write_explicit_memory
from zcaisagent.container import (
    build_cache,
    build_container,
    build_context,
    create_state,
    invalidate_retrieval_cache,
)
from zcaisagent.domain.enums import AutonomyLevel
from zcaisagent.domain.models import Message, RunState
from zcaisagent.orchestration.build import build_default_graph
from zcaisagent.orchestration.runtime import Runtime
from zcaisagent.ports.security import AuditEvent
from zcaisagent.scheduler.jobs import ContainerRunLauncher, load_jobs
from zcaisagent.scheduler.service import SchedulerService

# 多轮上下文窗口，与 api/service.py 保持一致。
_HISTORY_WINDOW = 6


async def _run(
    container,
    query: str,
    level: AutonomyLevel,
    thread_id: str | None = None,
    history: list[Message] | None = None,
) -> RunState:
    auth = await container.identity.authenticate(
        container.settings.api_key, {"tenant_id": container.settings.default_tenant}
    )
    state = create_state(
        container,
        goal=query,
        thread_id=thread_id,
        level=level,
        auth=auth,
        history=history,
    )
    ctx = build_context(container, auth)
    await write_explicit_memory(container.memory, state.goal, ctx)
    result = await Runtime().execute(build_default_graph(), state, ctx)
    await distill_and_write(container.memory, result, ctx)
    return result


def build_scheduler_launcher(container) -> ContainerRunLauncher:
    """调度器触发 Run 的执行回调；容器装配细节留在 CLI 层。"""

    async def run_goal(goal: str, tenant_id: str, user_id: str, level: str) -> str:
        auth = await container.identity.authenticate(
            container.settings.api_key,
            {"tenant_id": tenant_id, "user_id": user_id},
        )
        state = create_state(
            container,
            goal=goal,
            tenant_id=tenant_id,
            user_id=user_id,
            level=_parse_level(level),
            auth=auth,
        )
        ctx = build_context(container, auth)
        await write_explicit_memory(container.memory, state.goal, ctx)
        result = await Runtime().execute(build_default_graph(), state, ctx)
        await distill_and_write(container.memory, result, ctx)
        await container.audit.append(
            AuditEvent(
                action="run.finished",
                actor=user_id,
                tenant_id=tenant_id,
                run_id=result.run_id,
                detail={
                    "status": result.status.value,
                    "steps": result.step,
                    "source": "scheduler",
                },
            )
        )
        return result.run_id

    return ContainerRunLauncher(run_goal)


def _parse_scheduler_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(ZoneInfo("Asia/Shanghai"))
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed


def cmd_scheduler_tick(args: argparse.Namespace) -> int:
    jobs = load_jobs(Path(args.config))
    container = build_container()
    now = _parse_scheduler_now(args.now)

    async def _tick():
        service = SchedulerService(
            build_scheduler_launcher(container),
            audit=container.audit,
            events=container.events,
        )
        for job in jobs:
            await service.upsert(job)
        return await service.tick(now)

    runs = asyncio.run(_tick())
    print(
        json.dumps(
            [run.model_dump(mode="json") for run in runs],
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _print_state(state: RunState) -> None:
    answer = state.answer
    print(f"run_id: {state.run_id}")
    print(f"status: {state.status.value}  steps: {state.step}")
    print(f"answer: {answer.text if answer else ''}")
    print(f"confidence: {answer.confidence if answer else 'low'}")
    _print_usage(state)
    if answer:
        for citation in answer.citations:
            print(f"  [{citation.evidence_id}] confidence={citation.confidence}")
        for limitation in answer.limitations:
            print(f"  limitation: {limitation}")
    print(f"trace: {container_path(state)}")


def _print_usage(state: RunState) -> None:
    budget = state.budget
    elapsed_s = (
        (state.finished_ts or time.time()) - state.created_ts
        if state.created_ts
        else 0.0
    )
    token_pct = (
        budget.used_tokens / budget.max_tokens * 100 if budget.max_tokens else 0.0
    )
    hit_pct = (
        budget.used_cached_tokens / budget.used_tokens_in * 100
        if budget.used_tokens_in
        else 0.0
    )
    print(f"elapsed: {elapsed_s:.2f}s  llm_calls: {budget.used_llm_calls}")
    print(
        f"tokens: in={budget.used_tokens_in} out={budget.used_tokens_out}"
        f" total={budget.used_tokens}/{budget.max_tokens} ({token_pct:.1f}%)"
    )
    print(
        f"cache: {budget.used_cached_tokens} hit"
        f" ({hit_pct:.1f}% of prompt tokens)"
    )


def container_path(state: RunState) -> str:
    return f".zcaisagent/trace.jsonl ({state.run_id})"


def cmd_ask(args: argparse.Namespace) -> int:
    container = build_container()
    level = _parse_level(args.level)
    state = asyncio.run(_run(container, args.query, level))
    _print_state(state)
    return 0 if state.status.value == "succeeded" else 1


def cmd_chat(args: argparse.Namespace) -> int:
    container = build_container()
    level = _parse_level(args.level)
    thread_id = f"cli_{uuid.uuid4().hex[:8]}"
    history: list[Message] = []
    print(f"zCAISAgent 交互模式（thread: {thread_id}），exit / quit 退出。")
    while True:
        try:
            line = input("\n你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line.lower() in {"exit", "quit", ":q"}:
            return 0
        state = asyncio.run(
            _run(container, line, level, thread_id=thread_id, history=history)
        )
        _print_state(state)
        history.append(Message(role="user", content=line))
        if state.answer is not None and not state.answer.abstained:
            history.append(Message(role="assistant", content=state.answer.text))
        history = history[-_HISTORY_WINDOW:]


def cmd_eval(args: argparse.Namespace) -> int:
    from zcaisagent.evaluation.report import build_report, write_report
    from zcaisagent.evaluation.runner import filter_cases, load_cases, run_dataset

    cases = load_cases(Path(args.path))
    cases = filter_cases(cases, tag=args.tag, level=args.level)
    container = build_container()
    report = asyncio.run(run_dataset(container, cases))
    if args.out:
        report = build_report(report, len(cases))
        write_report(report, Path(args.out))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_eval_gate(args: argparse.Namespace) -> int:
    from zcaisagent.evaluation.gate import check, load_thresholds
    from zcaisagent.evaluation.runner import filter_cases, load_cases, run_dataset

    thresholds = load_thresholds(Path(args.thresholds))
    if args.report:
        report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    else:
        cases = filter_cases(
            load_cases(Path(args.path)), tag=args.tag, level=args.level
        )
        report = asyncio.run(run_dataset(build_container(), cases))

    result = check(report, thresholds)
    print(
        json.dumps(
            {
                "passed": result.passed,
                "violations": result.violations,
                "thresholds": {
                    "correct_rate": thresholds.correct_rate,
                    "citation_support": thresholds.citation_support,
                    "faithfulness": thresholds.faithfulness,
                    "context_recall": thresholds.context_recall,
                    "abstain_accuracy": thresholds.abstain_accuracy,
                    "allow_failed": thresholds.allow_failed,
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result.passed else 1


def cmd_memory_eval(args: argparse.Namespace) -> int:
    from zcaisagent.evaluation.memory import load_memory_cases, run_memory_eval
    from zcaisagent.settings import Settings

    cases = load_memory_cases(Path(args.path))
    container = build_container(Settings())
    report = asyncio.run(run_memory_eval(container, cases))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_security_eval(args: argparse.Namespace) -> int:
    from zcaisagent.evaluation.security import load_security_cases, run_security_eval
    from zcaisagent.governance.guards import RuleGuardrail

    cases = load_security_cases(Path(args.path))
    report = asyncio.run(run_security_eval(RuleGuardrail(), cases))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["overall_pass_rate"] >= 0.9 else 1


def cmd_grade(args: argparse.Namespace) -> int:
    from zcaisagent.evaluation.grading import load_grading_cases, run_grading

    cases = load_grading_cases(Path(args.path))
    container = build_container()
    report = asyncio.run(run_grading(container, cases))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["pass"] else 1


def cmd_retrieval_eval(args: argparse.Namespace) -> int:
    from dataclasses import replace

    from zcaisagent.evaluation.retrieval import run_retrieval_eval
    from zcaisagent.evaluation.runner import load_cases
    from zcaisagent.settings import Settings

    cases = load_cases(Path(args.path))
    settings = Settings.from_env()
    if args.parent_child:
        settings = replace(settings, parent_child_enabled=True)
    container = build_container(settings)
    if args.source == "fused":
        retrievers = container.sources.list()
    else:
        retriever = container.sources.get(args.source)
        if retriever is None:
            print(json.dumps({"error": f"unknown source: {args.source}"}, ensure_ascii=False))
            return 1
        retrievers = [retriever]
    reranker = None
    if args.rerank:
        from zcaisagent.capabilities.retrieval.rerank import LLMPointwiseReranker

        reranker = LLMPointwiseReranker(
            container.llm, timeout_s=container.settings.node_timeout_s
        )
    hyde = None
    if args.hyde:
        from zcaisagent.capabilities.retrieval.rewrite import hyde_variant

        async def _hyde(query: str) -> str | None:
            response = await hyde_variant(
                container.llm,
                query,
                timeout_s=container.settings.node_timeout_s,
                max_tokens=container.settings.hyde_max_tokens,
            )
            return response.text.strip() if response is not None else None

        hyde = _hyde

    report = asyncio.run(
        run_retrieval_eval(
            retrievers,
            container.retriever.chunks,
            cases,
            top_k=args.top_k,
            config_name=(
                args.source
                + ("-rerank" if args.rerank else "")
                + ("-hyde" if args.hyde else "")
            ),
            reranker=reranker,
            hyde=hyde,
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    from zcaisagent.adapters.llm.openai_compat import OpenAICompatEmbedding
    from zcaisagent.adapters.store.postgres import PsycopgVectorStore, StoreUnavailable
    from zcaisagent.ingestion.pipeline import ingest_directory
    from zcaisagent.settings import Settings

    settings = Settings.from_env()
    database = settings.database
    if database is None or not database.enabled:
        print(json.dumps({"error": "database not enabled"}, ensure_ascii=False))
        return 1

    store = PsycopgVectorStore(database.dsn, database.dimensions)
    try:
        store.ensure_schema()
        if args.index_action == "stats":
            chunks = store.load_chunks()
            print(
                json.dumps(
                    {
                        "chunks": len(chunks),
                        "tenants": len({chunk.tenant_id for chunk in chunks}),
                        "dimensions": database.dimensions,
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        if settings.embedding is None:
            print(json.dumps({"error": "embedding not configured"}, ensure_ascii=False))
            return 1
        chunks = ingest_directory(
            settings.seed_dir,
            tenant_id=args.tenant or settings.default_tenant,
            parent_max_chars=(
                settings.parent_max_chars if settings.parent_child_enabled else None
            ),
        )
        embedder = OpenAICompatEmbedding(
            settings.embedding, timeout_s=settings.node_timeout_s
        )
        written = 0
        batch_size = 50
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            vectors = asyncio.run(embedder.embed([chunk.content for chunk in batch]))
            written += store.upsert(batch, vectors)
    except StoreUnavailable as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1

    # 索引已变更，旧检索缓存必须失效。
    cache = build_cache(settings)
    if cache is not None:
        asyncio.run(invalidate_retrieval_cache(cache))

    print(
        json.dumps(
            {"written": written, "dimensions": database.dimensions},
            ensure_ascii=False,
        )
    )
    return 0


def cmd_rerank_server(args: argparse.Namespace) -> int:
    from zcaisagent.rerank_server import main as server_main

    sys.argv = ["rerank-server", "--host", args.host, "--port", str(args.port)]
    return server_main()


def _parse_level(value: str | None) -> AutonomyLevel:
    if not value:
        return AutonomyLevel.L1_FAST
    try:
        return AutonomyLevel(value)
    except ValueError:
        return AutonomyLevel.L1_FAST


def main() -> int:
    parser = argparse.ArgumentParser(prog="zcaisagent")
    # 不指定子命令时默认进入交互对话。
    parser.set_defaults(func=cmd_chat, level="L1")
    sub = parser.add_subparsers(dest="command")

    ask = sub.add_parser("ask", help="ask a single question")
    ask.add_argument("query")
    ask.add_argument("--level", default="L1", choices=["L0", "L1", "L2", "L3"])
    ask.set_defaults(func=cmd_ask)

    chat = sub.add_parser("chat", help="interactive multi-turn chat")
    chat.add_argument("--level", default="L1", choices=["L0", "L1", "L2", "L3"])
    chat.set_defaults(func=cmd_chat)

    evaluate = sub.add_parser("eval", help="run golden set")
    evaluate.add_argument("path", nargs="?", default="data/eval/golden.jsonl")
    evaluate.add_argument("--tag", default=None, help="only run cases with this tag")
    evaluate.add_argument(
        "--level", default=None, choices=["L0", "L1", "L2", "L3"],
        help="only run cases at this level",
    )
    evaluate.add_argument(
        "--out", default=None, help="write the report JSON to this path"
    )
    evaluate.set_defaults(func=cmd_eval)

    grading = sub.add_parser("grade", help="run routing grading set")
    grading.add_argument("path", nargs="?", default="data/eval/grading.jsonl")
    grading.set_defaults(func=cmd_grade)

    gate = sub.add_parser("eval-gate", help="check evaluation report against thresholds")
    gate.add_argument("--report", default=None, help="existing report JSON")
    gate.add_argument(
        "--path", default="data/eval/golden.jsonl", help="golden dataset to run"
    )
    gate.add_argument("--tag", default=None, help="only run cases with this tag")
    gate.add_argument(
        "--level", default=None, choices=["L0", "L1", "L2", "L3"],
        help="only run cases at this level",
    )
    gate.add_argument(
        "--thresholds", default="configs/eval_gate.toml", help="threshold TOML"
    )
    gate.set_defaults(func=cmd_eval_gate)

    memory_eval = sub.add_parser(
        "memory-eval", help="run multi-turn memory recall/injection evaluation"
    )
    memory_eval.add_argument("path", nargs="?", default="data/eval/memory_cases.jsonl")
    memory_eval.set_defaults(func=cmd_memory_eval)

    security_eval = sub.add_parser(
        "security-eval", help="run prompt-injection red-team gate"
    )
    security_eval.add_argument("path", nargs="?", default="data/eval/security.jsonl")
    security_eval.set_defaults(func=cmd_security_eval)

    retrieval = sub.add_parser("retrieval-eval", help="run retrieval-only evaluation")
    retrieval.add_argument("path", nargs="?", default="data/eval/golden.jsonl")
    retrieval.add_argument(
        "--source", default="fused", help="fused (all sources) or a single source name"
    )
    retrieval.add_argument(
        "--rerank", action="store_true", help="apply LLM rerank after fusion"
    )
    retrieval.add_argument(
        "--hyde", action="store_true", help="add a HyDE query variant per case"
    )
    retrieval.add_argument(
        "--parent-child", action="store_true", help="ingest with parent-child chunks"
    )
    retrieval.add_argument("--top-k", type=int, default=10, help="retrieved chunks per query")
    retrieval.set_defaults(func=cmd_retrieval_eval)

    index = sub.add_parser("index", help="build or inspect the pgvector index")
    index.add_argument("index_action", choices=["build", "stats"])
    index.add_argument("--tenant", default=None)
    index.set_defaults(func=cmd_index)

    rerank_server = sub.add_parser(
        "rerank-server", help="run local /v1/rerank endpoint (Jina/Cohere compatible)"
    )
    rerank_server.add_argument("--host", default="127.0.0.1")
    rerank_server.add_argument("--port", type=int, default=8090)
    rerank_server.set_defaults(func=cmd_rerank_server)

    scheduler = sub.add_parser("scheduler", help="run scheduled jobs")
    scheduler_sub = scheduler.add_subparsers(dest="scheduler_action", required=True)
    tick = scheduler_sub.add_parser("tick", help="trigger jobs matching --now")
    tick.add_argument(
        "--now",
        default=None,
        help="ISO8601 time; defaults to now in Asia/Shanghai",
    )
    tick.add_argument(
        "--config", default="configs/schedules.toml", help="job definition TOML"
    )
    tick.set_defaults(func=cmd_scheduler_tick)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
