from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Any

from zcaisagent.capabilities.n2sql.errors import N2SQLError
from zcaisagent.capabilities.n2sql.permissions import resolve_scope
from zcaisagent.capabilities.n2sql.sql_guard import apply_scope, ensure_limit, validate_sql
from zcaisagent.domain.models import Message
from zcaisagent.ports.llm import LLMPort, LLMRequest
from zcaisagent.ports.n2sql import N2SQLPort, N2SQLResult, SqlQueryPort
from zcaisagent.ports.security import AuditEvent, AuditPort, AuthContext
from zcaisagent.settings import N2SQLConfig

_GENERATE_SYSTEM = (
    "你是 MySQL 数据分析助手。只输出一个 JSON 对象："
    '{"sql": string, "explanation": string}。'
    "规则：只使用给定表和列；只输出单条 SELECT/WITH；禁止多语句、注释、DDL/DML、"
    "系统库与危险函数；多表查询必须显式 JOIN，禁止逗号连接；"
    "除非用户明确要求“前 N 名/前 N 条”，否则不要自行添加 LIMIT，系统会统一限制行数；"
    "不要自行添加数据权限过滤，系统会在执行前强制注入。"
)
_ANSWER_SYSTEM = (
    "你是数据分析助手。只根据给定的 SQL 查询结果回答，不要编造结果中没有的数字；"
    "用中文简洁作答，必要时用短列表。"
)
_JSON_OBJECT = re.compile(r"\{.*\}", re.S)
_SQL_FIELD = re.compile(r'"sql"\s*:\s*"((?:[^"\\]|\\.)*)"', re.S)
_SQL_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", re.S | re.IGNORECASE)
_MAX_ROWS_IN_PROMPT = 30
_MAX_PROMPT_CHARS = 6000


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _parse_sql(text: str) -> str | None:
    match = _JSON_OBJECT.search(text)
    if match:
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("sql"), str):
            statement = payload["sql"].strip()
            if statement:
                return statement
    # JSON 被思考 token 截断时，sql 字段可能仍然完整。
    field = _SQL_FIELD.search(text)
    if field:
        try:
            statement = json.loads(f'"{field.group(1)}"').strip()
        except json.JSONDecodeError:
            statement = field.group(1).strip()
        if statement:
            return statement
    fenced = _SQL_FENCE.search(text)
    if fenced:
        statement = fenced.group(1).strip()
        if statement:
            return statement
    statement = text.strip()
    return statement if re.match(r"(?is)^\s*(select|with)\b", statement) else None


def _fallback_answer(rows: list[dict], max_rows: int) -> str:
    if not rows:
        return "未查询到符合条件的数据。"
    preview = json.dumps(rows[:5], ensure_ascii=False, default=str)
    suffix = (
        f"（仅展示前 5 行，共 {len(rows)} 行）"
        if len(rows) > 5
        else f"（共 {len(rows)} 行）"
    )
    return f"查询结果：{preview}{suffix}"


class N2SQLService(N2SQLPort):
    """自然语言 → SQL → 行级权限过滤 → 只读执行 → 自然语言答案。"""

    def __init__(
        self,
        config: N2SQLConfig,
        llm: LLMPort,
        executor: SqlQueryPort,
        *,
        audit: AuditPort | None = None,
        llm_timeout_s: float = 30.0,
    ) -> None:
        self._config = config
        self._llm = llm
        self._executor = executor
        self._audit = audit
        self._llm_timeout_s = llm_timeout_s

    async def ask(self, question: str, auth: AuthContext) -> N2SQLResult:
        started = time.monotonic()
        question = question.strip()
        if not question:
            return self._fail("N2SQL_INVALID_ARGS", "问题不能为空", started)
        if "tool:read" not in auth.scopes:
            return self._fail("N2SQL_FORBIDDEN", "缺少 tool:read 权限", started)

        scope = resolve_scope(self._config, auth)
        if scope is None:
            await self._record(auth, "forbidden", "", 0, scope="")
            return self._fail(
                "N2SQL_FORBIDDEN",
                f"用户 {auth.user_id} 未配置数据权限，已拒绝查询",
                started,
            )

        tokens_in = tokens_out = 0
        cost = 0.0
        llm_trace: list[dict[str, Any]] = []
        try:
            generated, usage, record = await self._generate(question)
            llm_trace.append(record)
        except Exception:  # noqa: BLE001 - 模型失败不向上抛
            await self._record(auth, "llm_failed", "", 0, scope=scope.label)
            return self._fail("N2SQL_LLM_FAILED", "SQL 生成失败，请稍后重试", started)
        tokens_in += usage[0]
        tokens_out += usage[1]
        cost += usage[2]
        if generated is None:
            await self._record(auth, "llm_failed", "", 0, scope=scope.label)
            return self._fail(
                "N2SQL_LLM_FAILED", "未能从模型输出中解析 SQL", started, llm_trace
            )

        try:
            sql = validate_sql(generated, self._config.tables)
            sql = apply_scope(sql, scope)
            sql = ensure_limit(sql, self._config.max_rows)
        except N2SQLError as exc:
            await self._record(auth, "invalid_sql", generated, 0, scope=scope.label)
            return self._fail(exc.code, exc.message, started, llm_trace)

        try:
            columns, rows = await asyncio.wait_for(
                self._executor.execute(sql, self._config.max_rows),
                timeout=self._config.timeout_s + 1.0,
            )
        except Exception:  # noqa: BLE001 - 数据库错误统一降级
            await self._record(auth, "db_failed", sql, 0, scope=scope.label)
            return self._fail(
                "N2SQL_DB_FAILED", "数据库查询失败", started, llm_trace
            )

        llm_calls = 1
        answer = _fallback_answer(rows, self._config.max_rows)
        if rows:
            llm_calls += 1
            try:
                answer, usage, record = await self._answer(question, sql, columns, rows)
                llm_trace.append(record)
            except Exception:  # noqa: BLE001 - 归纳失败回退结构化摘要
                usage = (0, 0, 0.0)
            tokens_in += usage[0]
            tokens_out += usage[1]
            cost += usage[2]

        await self._record(auth, "ok", sql, len(rows), scope=scope.label)
        return N2SQLResult(
            ok=True,
            answer=answer,
            sql=sql,
            columns=columns,
            rows=rows,
            scope=scope.label,
            row_limit=self._config.max_rows,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=round(cost, 6),
            llm_calls=llm_calls,
            latency_ms=_elapsed_ms(started),
            llm_trace=llm_trace,
        )

    async def _generate(
        self, question: str
    ) -> tuple[str | None, tuple[int, int, float], dict[str, Any]]:
        started = time.monotonic()
        schema = self._config.schema.strip()
        tables = ", ".join(self._config.tables)
        content = (
            f"表结构：\n{schema}\n\n可用表：{tables}\n\n问题：{question}"
            if schema
            else f"可用表：{tables}\n\n问题：{question}"
        )
        response = await self._llm.complete(
            LLMRequest(
                messages=[
                    Message(role="system", content=_GENERATE_SYSTEM),
                    Message(role="user", content=content),
                ],
                purpose="n2sql_generate",
                max_tokens=2048,
                timeout_s=self._llm_timeout_s,
                disable_thinking=True,
            )
        )
        usage = (response.tokens_in, response.tokens_out, response.cost_usd)
        record = {
            "purpose": "n2sql_generate",
            "model": response.model,
            "system": _GENERATE_SYSTEM,
            "prompt": content,
            "response": response.text,
            "tokens": response.tokens_in + response.tokens_out,
            "cost_usd": round(response.cost_usd, 6),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        return _parse_sql(response.text), usage, record

    async def _answer(
        self, question: str, sql: str, columns: list[str], rows: list[dict]
    ) -> tuple[str, tuple[int, int, float], dict[str, Any]]:
        started = time.monotonic()
        payload = json.dumps(
            {"columns": columns, "rows": rows[:_MAX_ROWS_IN_PROMPT]},
            ensure_ascii=False,
            default=str,
        )[:_MAX_PROMPT_CHARS]
        content = (
            f"问题：{question}\n\nSQL：{sql}\n\n查询结果：{payload}\n\n"
            "请基于查询结果直接回答。"
        )
        response = await self._llm.complete(
            LLMRequest(
                messages=[
                    Message(role="system", content=_ANSWER_SYSTEM),
                    Message(role="user", content=content),
                ],
                purpose="n2sql_answer",
                max_tokens=1024,
                timeout_s=self._llm_timeout_s,
                disable_thinking=True,
            )
        )
        answer = response.text.strip()
        if not answer:
            answer = _fallback_answer(rows, self._config.max_rows)
        record = {
            "purpose": "n2sql_answer",
            "model": response.model,
            "system": _ANSWER_SYSTEM,
            "prompt": content,
            "response": response.text,
            "tokens": response.tokens_in + response.tokens_out,
            "cost_usd": round(response.cost_usd, 6),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        return answer, (response.tokens_in, response.tokens_out, response.cost_usd), record

    async def _record(
        self,
        auth: AuthContext,
        outcome: str,
        sql: str,
        row_count: int,
        *,
        scope: str,
    ) -> None:
        if self._audit is None:
            return
        try:
            await self._audit.append(
                AuditEvent(
                    action="n2sql.query",
                    actor=auth.user_id,
                    tenant_id=auth.tenant_id,
                    detail={
                        "outcome": outcome,
                        "scope": scope,
                        "sql_digest": _digest(sql) if sql else "",
                        "row_count": row_count,
                    },
                )
            )
        except Exception:  # noqa: BLE001 - 审计失败不阻断查询
            pass

    def _fail(
        self,
        code: str,
        message: str,
        started: float,
        llm_trace: list[dict[str, Any]] | None = None,
    ) -> N2SQLResult:
        return N2SQLResult(
            ok=False,
            error_code=code,
            error_message=message,
            latency_ms=_elapsed_ms(started),
            llm_trace=list(llm_trace or []),
        )
