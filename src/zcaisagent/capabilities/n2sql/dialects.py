"""SQL 方言差异：标识符引用、行数限制与危险对象规则。"""
from __future__ import annotations

import re
from typing import Literal

Dialect = Literal["mysql", "postgres", "sqlite", "oracle", "mssql"]

DIALECTS: tuple[Dialect, ...] = ("mysql", "postgres", "sqlite", "oracle", "mssql")

_ALIASES = {
    "mysql": "mysql",
    "mariadb": "mysql",
    "postgres": "postgres",
    "postgresql": "postgres",
    "pg": "postgres",
    "sqlite": "sqlite",
    "sqlite3": "sqlite",
    "oracle": "oracle",
    "ora": "oracle",
    "mssql": "mssql",
    "sqlserver": "mssql",
    "sql_server": "mssql",
    "sql-server": "mssql",
}

_LABELS = {
    "mysql": "MySQL",
    "postgres": "PostgreSQL",
    "sqlite": "SQLite",
    "oracle": "Oracle",
    "mssql": "SQL Server",
}

# 统一按“方言 → 禁止访问的系统对象”维护，避免模型读取元数据或越权库。
_SYSTEM_OBJECTS = {
    "mysql": r"\b(information_schema|performance_schema|mysql|sys)\s*\.",
    "postgres": r"\b(information_schema|pg_catalog|pg_toast)\s*\.",
    "sqlite": r"\b(sqlite_master|sqlite_schema|sqlite_temp_master|sqlite_temp_schema)\b",
    "oracle": r"\b(sys|system)\s*\.|\bv\$",
    "mssql": r"\b(sys|information_schema|master|msdb)\s*\.|\bxp_",
}

# 只读之外的“副作用函数”：读文件、延时、命令执行、网络访问。
_DANGEROUS_FUNCTIONS = {
    "mysql": r"\b(load_file|sleep|benchmark)\s*\(",
    "postgres": (
        r"\b(pg_read_file|pg_read_binary_file|pg_ls_dir|pg_sleep|pg_stat_file"
        r"|lo_import|lo_export|dblink)\s*\("
    ),
    "sqlite": r"\b(load_extension|readfile|writefile)\s*\(",
    "oracle": r"\b(utl_http|utl_file|dbms_lock|dbms_pipe|dbms_scheduler)\s*\.",
    "mssql": r"\b(xp_cmdshell|openrowset|opendatasource|sp_oacreate)\b",
}

_PROMPT_HINTS = {
    "mysql": (
        "标识符用反引号，限制行数用 LIMIT，当前日期用 CURDATE()，"
        "禁止访问 mysql/sys 等系统库。"
    ),
    "postgres": (
        "标识符用双引号，限制行数用 LIMIT，当前日期用 CURRENT_DATE，"
        "日期截断用 DATE_TRUNC，禁止访问 pg_catalog。"
    ),
    "sqlite": (
        "标识符用双引号，限制行数用 LIMIT，当前日期用 DATE('now')，"
        "禁止访问 sqlite_master。"
    ),
    "oracle": (
        "标识符用双引号，限制行数用 FETCH FIRST n ROWS ONLY，当前日期用 SYSDATE，"
        "禁止访问 V$、SYS、SYSTEM 与 DBMS_ 包。"
    ),
    "mssql": (
        "标识符用方括号，限制行数用 TOP 或 OFFSET/FETCH，当前日期用 CAST(GETDATE() AS date)，"
        "禁止访问 sys、master、msdb 与 xp_ 过程。"
    ),
}


def normalize_dialect(value: str) -> Dialect:
    key = str(value or "").strip().lower()
    dialect = _ALIASES.get(key)
    if dialect is None:
        raise ValueError(f"unsupported sql dialect: {value}")
    return dialect  # type: ignore[return-value]


def dialect_label(dialect: str) -> str:
    return _LABELS.get(dialect, dialect)


def quote_identifier(dialect: str, name: str) -> str:
    if dialect == "mysql":
        return f"`{name.replace('`', '``')}`"
    if dialect == "mssql":
        return f"[{name.replace(']', ']]')}]"
    quote = '"'
    return f'"{name.replace(quote, quote * 2)}"'


def has_limit(dialect: str, sql: str) -> bool:
    if dialect == "oracle":
        return bool(re.search(r"\bfetch\s+first\b|\brownum\b", sql, re.IGNORECASE))
    if dialect == "mssql":
        return bool(re.search(r"\btop\b|\boffset\b", sql, re.IGNORECASE))
    return bool(re.search(r"\blimit\b", sql, re.IGNORECASE))


def add_limit(dialect: str, sql: str, max_rows: int) -> str:
    """按方言追加行数限制；SQL Server 由执行层 fetchmany 截断。"""
    limit = max(1, int(max_rows))
    if dialect == "oracle":
        return f"{sql} FETCH FIRST {limit} ROWS ONLY"
    if dialect == "mssql":
        return sql
    return f"{sql} LIMIT {limit}"


def forbidden_objects(dialect: str) -> re.Pattern[str]:
    pattern = _SYSTEM_OBJECTS.get(dialect)
    dangerous = _DANGEROUS_FUNCTIONS.get(dialect)
    parts = [item for item in (pattern, dangerous) if item]
    if not parts:
        return re.compile(r"(?!x)x")
    return re.compile("|".join(f"(?:{item})" for item in parts), re.IGNORECASE)


def prompt_hint(dialect: str) -> str:
    label = dialect_label(dialect)
    hint = _PROMPT_HINTS.get(dialect, "只使用标准 SQL。")
    return f"当前数据库方言：{label}。{hint}"
