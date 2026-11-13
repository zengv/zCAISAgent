from __future__ import annotations

import re

from zcaisagent.capabilities.n2sql.errors import N2SQLError
from zcaisagent.ports.n2sql import DataScope

_STATEMENT_HEAD = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_COMMENT = re.compile(r"--|#|/\*")
_WRITE_KEYWORDS = re.compile(
    r"\b(pragma|attach|detach|insert|update|delete|drop|alter|create|replace"
    r"|vacuum|reindex|begin|commit|rollback|set|call|do|handler|load|grant"
    r"|revoke|use|show|describe|explain|into|outfile|dumpfile|procedure"
    r"|function|trigger|event|lock|unlock|rename|truncate)\b",
    re.IGNORECASE,
)
_FORBIDDEN_OBJECTS = re.compile(
    r"\b(information_schema|performance_schema|mysql|sys)\s*\."
    r"|\b(load_file|sleep|benchmark)\s*\(",
    re.IGNORECASE,
)
_CTE_NAMES = re.compile(
    r"(?:\bwith\b|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s+as\s*\(", re.IGNORECASE
)
_TABLES = re.compile(
    r"\b(?:from|join)\s+([`\"\[]?[A-Za-z_][A-Za-z0-9_.]*[`\"\]]?)", re.IGNORECASE
)
_HAS_LIMIT = re.compile(r"\blimit\b", re.IGNORECASE)

_TABLE_REF = re.compile(
    r"\b(FROM|JOIN)\s+(?:`?[A-Za-z_][A-Za-z0-9_]*`?\.)?`?(courses|students|scores)`?"
    r"(\s+(?:AS\s+)?(`?[A-Za-z_][A-Za-z0-9_]*`?))?",
    re.IGNORECASE,
)
_COMMA_JOIN = re.compile(
    r",\s*(?:`?[A-Za-z_][A-Za-z0-9_]*`?\.)?`?(courses|students|scores)`?\b",
    re.IGNORECASE,
)
_RESERVED_ALIAS = {
    "where", "join", "left", "right", "inner", "outer", "cross", "full",
    "natural", "straight_join", "on", "using", "group", "order", "limit",
    "having", "union", "window", "for", "lock", "set", "and", "or",
}


def validate_sql(sql: str, allowed_tables: list[str]) -> str:
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
    if _FORBIDDEN_OBJECTS.search(statement):
        raise N2SQLError("N2SQL_INVALID_SQL", "SQL 访问了禁止的系统对象或函数")
    if _COMMA_JOIN.search(statement):
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


def _literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _predicate(table: str, scope: DataScope) -> str | None:
    if scope.role == "admin":
        return None
    if table == "courses":
        if scope.role == "teacher" and scope.teacher_name:
            return f"courseTeacher = {_literal(scope.teacher_name)}"
        return None
    if table == "scores":
        if scope.role == "student" and scope.student_id:
            return f"studentID = {_literal(scope.student_id)}"
        if scope.role == "teacher" and scope.teacher_name:
            teacher = _literal(scope.teacher_name)
            return (
                "courseID IN (SELECT courseID FROM courses "
                f"WHERE courseTeacher = {teacher})"
            )
        return None
    if table == "students":
        if scope.role == "student" and scope.student_id:
            return f"studentID = {_literal(scope.student_id)}"
        if scope.role == "teacher" and scope.teacher_name:
            teacher = _literal(scope.teacher_name)
            return (
                "studentID IN (SELECT studentID FROM scores WHERE courseID IN "
                "(SELECT courseID FROM courses "
                f"WHERE courseTeacher = {teacher}))"
            )
    return None


def apply_scope(sql: str, scope: DataScope) -> str:
    """把白名单表替换为带行级过滤的派生表；模型生成的 SQL 无法绕过。"""

    def rewrite(match: re.Match[str]) -> str:
        keyword = match.group(1)
        table = match.group(2).lower()
        alias = (match.group(4) or "").strip("`")
        if alias.lower() in _RESERVED_ALIAS:
            alias = ""
        predicate = _predicate(table, scope)
        if predicate is None:
            return match.group(0)
        inner = f"SELECT * FROM `{table}` WHERE {predicate}"
        return f"{keyword} ({inner}) AS {alias or table}"

    return _TABLE_REF.sub(rewrite, sql)


def ensure_limit(sql: str, max_rows: int) -> str:
    if _HAS_LIMIT.search(sql):
        return sql
    return f"{sql} LIMIT {max(1, int(max_rows))}"
