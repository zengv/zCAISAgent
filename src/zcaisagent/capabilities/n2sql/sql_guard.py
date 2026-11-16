from __future__ import annotations

import re

from zcaisagent.capabilities.n2sql.dialects import (
    add_limit,
    forbidden_objects,
    has_limit,
    quote_identifier,
)
from zcaisagent.capabilities.n2sql.errors import N2SQLError
from zcaisagent.ports.n2sql import DataScope

_STATEMENT_HEAD = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_COMMENT = re.compile(r"--|#|/\*")
_WRITE_KEYWORDS = re.compile(
    r"\b(pragma|attach|detach|insert|update|delete|drop|alter|create|replace"
    r"|vacuum|reindex|begin|commit|rollback|set|call|do|handler|load|grant"
    r"|revoke|use|show|describe|explain|into|outfile|dumpfile|procedure"
    r"|function|trigger|event|lock|unlock|rename|truncate|copy|merge|exec"
    r"|execute|backup|restore|shutdown|dbcc)\b",
    re.IGNORECASE,
)
_CTE_NAMES = re.compile(
    r"(?:\bwith\b|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s+as\s*\(", re.IGNORECASE
)
_TABLES = re.compile(
    r"\b(?:from|join)\s+([`\"\[]?[A-Za-z_][A-Za-z0-9_.]*[`\"\]]?)", re.IGNORECASE
)
_IDENT = r"[`\"\[]?[A-Za-z_][A-Za-z0-9_]*[`\"\]]?"

_RESERVED_ALIAS = {
    "where", "join", "left", "right", "inner", "outer", "cross", "full",
    "natural", "straight_join", "on", "using", "group", "order", "limit",
    "having", "union", "window", "for", "lock", "set", "and", "or",
    "fetch", "offset", "top",
}
_DEFAULT_TABLES = ("courses", "students", "scores")


def _table_ref_pattern(tables: tuple[str, ...] | list[str]) -> re.Pattern[str]:
    names = "|".join(re.escape(name) for name in tables)
    target = f"(?:{names})" if names else _IDENT
    return re.compile(
        rf"\b(FROM|JOIN)\s+(?:(?P<schema>{_IDENT})\.)?(?P<table>{target})"
        rf"(?:\s+(?:AS\s+)?(?P<alias>{_IDENT}))?",
        re.IGNORECASE,
    )


def _comma_join_pattern(tables: tuple[str, ...] | list[str]) -> re.Pattern[str] | None:
    names = "|".join(re.escape(name) for name in tables)
    if not names:
        return None
    return re.compile(rf",\s*(?:{_IDENT}\.)?(?:{names})\b", re.IGNORECASE)


def validate_sql(
    sql: str, allowed_tables: list[str], dialect: str = "mysql"
) -> str:
    """只读 SQL 校验：单条 SELECT/WITH、表白名单、禁止注释与危险对象。"""
    statement = sql.strip().rstrip(";").strip()
    if not statement:
        raise N2SQLError("N2SQL_INVALID_SQL", "SQL 为空")
    if ";" in statement:
        raise N2SQLError("N2SQL_INVALID_SQL", "只允许单条 SQL")
    if _COMMENT.search(statement):
        raise N2SQLError("N2SQL_INVALID_SQL", "SQL 中不允许注释")
    if not _STATEMENT_HEAD.match(statement):
        raise N2SQLError("N2SQL_INVALID_SQL", "只允许 SELECT/WITH 查询")
    if _WRITE_KEYWORDS.search(statement):
        raise N2SQLError("N2SQL_INVALID_SQL", "SQL 包含写操作或危险关键字")
    if forbidden_objects(dialect).search(statement):
        raise N2SQLError("N2SQL_INVALID_SQL", "SQL 访问了禁止的系统对象或函数")
    comma_join = _comma_join_pattern(allowed_tables)
    if comma_join is not None and comma_join.search(statement):
        raise N2SQLError("N2SQL_INVALID_SQL", "多表查询请使用显式 JOIN")

    whitelist = {name.split(".")[-1].lower() for name in allowed_tables}
    cte_names = {name.lower() for name in _CTE_NAMES.findall(statement)}
    if cte_names & whitelist:
        raise N2SQLError("N2SQL_INVALID_SQL", "CTE 名称不能与表白名单重名")
    tables = {
        raw.strip("`\"[]").split(".")[-1].lower()
        for raw in _TABLES.findall(statement)
    }
    if not tables:
        raise N2SQLError("N2SQL_INVALID_SQL", "无法解析查询表名")
    unknown = sorted(name for name in tables if name not in whitelist and name not in cte_names)
    if unknown:
        raise N2SQLError("N2SQL_INVALID_SQL", f"表不在白名单中: {','.join(unknown)}")
    return statement


def _literal(value: str, dialect: str = "mysql") -> str:
    if dialect == "mysql":
        escaped = value.replace("\\", "\\\\").replace("'", "''")
    else:
        escaped = value.replace("'", "''")
    return "'" + escaped + "'"


def _predicate(table: str, scope: DataScope, dialect: str = "mysql") -> str | None:
    if scope.role == "admin":
        return None
    quoted = quote_identifier(dialect, table)
    if table == "courses":
        if scope.role == "teacher" and scope.teacher_name:
            return f"courseTeacher = {_literal(scope.teacher_name, dialect)}"
        return None
    if table == "scores":
        if scope.role == "student" and scope.student_id:
            return f"studentID = {_literal(scope.student_id, dialect)}"
        if scope.role == "teacher" and scope.teacher_name:
            teacher = _literal(scope.teacher_name, dialect)
            return (
                f"courseID IN (SELECT courseID FROM {quoted} "
                f"WHERE courseTeacher = {teacher})"
            )
        return None
    if table == "students":
        if scope.role == "student" and scope.student_id:
            return f"studentID = {_literal(scope.student_id, dialect)}"
        if scope.role == "teacher" and scope.teacher_name:
            teacher = _literal(scope.teacher_name, dialect)
            return (
                f"studentID IN (SELECT studentID FROM {quote_identifier(dialect, 'scores')} "
                f"WHERE courseID IN (SELECT courseID FROM {quoted} "
                f"WHERE courseTeacher = {teacher}))"
            )
    return None


def apply_scope(
    sql: str,
    scope: DataScope,
    dialect: str = "mysql",
    tables: tuple[str, ...] | list[str] | None = None,
) -> str:
    """把白名单表替换为带行级过滤的派生表；模型生成的 SQL 无法绕过。"""
    if scope.role == "admin":
        return sql

    pattern = _table_ref_pattern(tables or _DEFAULT_TABLES)

    def rewrite(match: re.Match[str]) -> str:
        keyword = match.group(1)
        table = match.group("table").strip("`\"[]")
        alias_raw = match.group("alias") or ""
        alias = alias_raw.strip("`\"[]")
        reserved = alias.lower() in _RESERVED_ALIAS
        if reserved:
            alias = ""
        predicate = _predicate(table.lower(), scope, dialect)
        if predicate is None:
            return match.group(0)
        inner = f"SELECT * FROM {quote_identifier(dialect, table)} WHERE {predicate}"
        tail = f" {alias_raw}" if reserved else ""
        return f"{keyword} ({inner}) AS {alias or table}{tail}"

    return pattern.sub(rewrite, sql)


def ensure_limit(sql: str, max_rows: int, dialect: str = "mysql") -> str:
    if has_limit(dialect, sql):
        return sql
    return add_limit(dialect, sql, max_rows)
