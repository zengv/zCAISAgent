"""SQLite 只读查询工具：单条 SELECT/WITH、表白名单、参数化、行数上限。"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote

from zcaisagent.ports.security import AuthContext
from zcaisagent.ports.tools import ToolResult, ToolSpec

_STATEMENT_HEAD = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_WRITE_KEYWORDS = re.compile(
    r"\b(pragma|attach|detach|insert|update|delete|drop|alter|create|replace"
    r"|vacuum|reindex|begin|commit|rollback)\b",
    re.IGNORECASE,
)
_CTE_NAMES = re.compile(
    r"(?:\bwith\b|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s+as\s*\(", re.IGNORECASE
)
_TABLES = re.compile(
    r"\b(?:from|join)\s+([`\"\[]?[A-Za-z_][A-Za-z0-9_.]*[`\"\]]?)", re.IGNORECASE
)
_HAS_LIMIT = re.compile(r"\blimit\b", re.IGNORECASE)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


class SqlReadonlyTool:
    """只读 SQLite 工具；每次调用新建只读连接，避免跨线程共享。"""

    def __init__(
        self,
        db_path: str | Path,
        tables: list[str],
        max_rows: int = 100,
        name: str = "sql_readonly",
    ) -> None:
        resolved = Path(db_path).expanduser().resolve()
        self._uri = f"file:{quote(resolved.as_posix(), safe='/:')}?mode=ro"
        self._tables = {str(table).split(".")[-1].lower() for table in tables}
        self._max_rows = max(1, int(max_rows))
        self.spec = ToolSpec(
            name=name,
            description=(
                f"只读 SQL 查询（表白名单：{', '.join(sorted(self._tables))}），"
                f"最多返回 {self._max_rows} 行。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "单条 SELECT/WITH 语句"},
                    "params": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "位置参数，按 ? 顺序绑定",
                    },
                },
                "required": ["sql"],
                "additionalProperties": False,
            },
            risk_level="read",
            scopes=["tool:read"],
            timeout_s=10,
            idempotent=True,
        )

    def _reject_reason(self, sql: str) -> str:
        statement = sql.strip()
        if not statement:
            return "empty sql"
        if ";" in statement:
            return "multiple statements are not allowed"
        if not _STATEMENT_HEAD.match(statement):
            return "only SELECT/WITH statements are allowed"
        if _WRITE_KEYWORDS.search(statement):
            return "write or pragma keywords are not allowed"
        cte_names = {name.lower() for name in _CTE_NAMES.findall(statement)}
        tables = {
            raw.strip("`\"[]").split(".")[-1].lower()
            for raw in _TABLES.findall(statement)
        }
        if not tables:
            return "cannot resolve table name"
        unknown = sorted(
            name for name in tables if name not in self._tables and name not in cte_names
        )
        if unknown:
            return "table not allowed: " + ",".join(unknown)
        return ""

    async def invoke(self, args: dict, ctx: AuthContext) -> ToolResult:
        started = time.monotonic()
        sql = str(args.get("sql") or "")
        reason = self._reject_reason(sql)
        if reason:
            return ToolResult(
                tool=self.spec.name,
                ok=False,
                error="TOOL_INVALID_ARGS",
                latency_ms=_elapsed_ms(started),
            )

        params = args.get("params") or []
        if not isinstance(params, list):
            return ToolResult(
                tool=self.spec.name,
                ok=False,
                error="TOOL_INVALID_ARGS",
                latency_ms=_elapsed_ms(started),
            )

        statement = sql.strip()
        if not _HAS_LIMIT.search(statement):
            statement = f"{statement} LIMIT {self._max_rows}"

        try:
            connection = sqlite3.connect(
                self._uri, uri=True, check_same_thread=False, timeout=5.0
            )
            try:
                cursor = connection.execute(statement, tuple(params))
                rows = cursor.fetchmany(self._max_rows)
                columns = [item[0] for item in cursor.description or []]
            finally:
                connection.close()
        except sqlite3.Error:
            return ToolResult(
                tool=self.spec.name,
                ok=False,
                error="TOOL_FAILED",
                latency_ms=_elapsed_ms(started),
            )

        payload = [
            dict(zip(columns, row, strict=False))
            for row in rows
        ]
        return ToolResult(
            tool=self.spec.name,
            ok=True,
            output=json.dumps(payload, ensure_ascii=False, default=str),
            latency_ms=_elapsed_ms(started),
        )
