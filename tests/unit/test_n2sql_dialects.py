import asyncio
import json
import sqlite3

import pytest

from zcaisagent.adapters.store.sqlite_query import SqliteReadonlyClient
from zcaisagent.capabilities.n2sql.dialects import (
    add_limit,
    normalize_dialect,
    prompt_hint,
    quote_identifier,
)
from zcaisagent.capabilities.n2sql.errors import N2SQLError
from zcaisagent.capabilities.n2sql.files import FileTableExecutor
from zcaisagent.capabilities.n2sql.sql_guard import apply_scope, ensure_limit, validate_sql
from zcaisagent.ports.n2sql import DataScope

TABLES = ["courses", "students", "scores"]


def test_normalize_dialect_accepts_aliases() -> None:
    assert normalize_dialect("postgresql") == "postgres"
    assert normalize_dialect("MariaDB") == "mysql"
    assert normalize_dialect("sqlserver") == "mssql"
    with pytest.raises(ValueError):
        normalize_dialect("db2")


def test_quote_identifier_per_dialect() -> None:
    assert quote_identifier("mysql", "score") == "`score`"
    assert quote_identifier("postgres", "score") == '"score"'
    assert quote_identifier("mssql", "score") == "[score]"
    assert quote_identifier("postgres", 'a"b') == '"a""b"'


def test_limit_clause_per_dialect() -> None:
    assert add_limit("postgres", "SELECT 1", 10).endswith("LIMIT 10")
    assert add_limit("oracle", "SELECT 1", 10).endswith("FETCH FIRST 10 ROWS ONLY")
    assert add_limit("mssql", "SELECT 1", 10) == "SELECT 1"
    assert ensure_limit("SELECT 1", 10, "oracle").endswith("FETCH FIRST 10 ROWS ONLY")
    assert ensure_limit("SELECT TOP 5 * FROM scores", 10, "mssql") == (
        "SELECT TOP 5 * FROM scores"
    )
    assert prompt_hint("postgres").startswith("当前数据库方言：PostgreSQL")


def test_validate_rejects_dialect_specific_system_objects() -> None:
    assert validate_sql("SELECT * FROM scores", TABLES, "postgres")
    assert validate_sql("SELECT * FROM scores", TABLES, "mssql")
    with pytest.raises(N2SQLError):
        validate_sql("SELECT pg_sleep(1) FROM scores", TABLES, "postgres")
    with pytest.raises(N2SQLError):
        validate_sql("SELECT * FROM sys.tables", TABLES, "mssql")
    with pytest.raises(N2SQLError):
        validate_sql("SELECT * FROM scores INTO OUTFILE '/tmp/x'", TABLES, "mysql")
    with pytest.raises(N2SQLError):
        validate_sql("COPY scores TO '/tmp/x'", TABLES, "postgres")


def test_apply_scope_quotes_and_keeps_where_clause() -> None:
    scope = DataScope(role="student", label="S1", student_id="S1")
    sql = apply_scope("SELECT * FROM scores WHERE score > 60", scope, "postgres")
    assert '"scores"' in sql
    assert "studentID = 'S1'" in sql
    assert "WHERE score > 60" in sql
    mssql = apply_scope("SELECT * FROM scores", scope, "mssql")
    assert "[scores]" in mssql


def test_file_table_executor_loads_csv_and_json(tmp_path) -> None:
    (tmp_path / "sales.csv").write_text(
        "region,amount\n华东,100\n华南,200\n", encoding="utf-8"
    )
    (tmp_path / "people.json").write_text(
        json.dumps([{"name": "张三", "age": 20}], ensure_ascii=False),
        encoding="utf-8",
    )
    executor = FileTableExecutor(tmp_path)
    assert set(executor.tables) == {"sales", "people"}
    assert "CREATE TABLE \"sales\"" in executor.schema
    columns, rows = asyncio.run(
        executor.execute('SELECT region, amount FROM "sales" ORDER BY amount DESC', 10)
    )
    assert columns == ["region", "amount"]
    assert rows[0] == {"region": "华南", "amount": 200}


def test_file_table_executor_is_read_only(tmp_path) -> None:
    (tmp_path / "sales.csv").write_text("region,amount\n华东,100\n", encoding="utf-8")
    executor = FileTableExecutor(tmp_path)
    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(executor.execute('DROP TABLE "sales"', 10))


def test_sqlite_readonly_client_executes(tmp_path) -> None:
    path = tmp_path / "demo.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE t (a INTEGER)")
    connection.executemany("INSERT INTO t VALUES (?)", [(1,), (2,)])
    connection.commit()
    connection.close()

    client = SqliteReadonlyClient(database=str(path))
    columns, rows = asyncio.run(client.execute("SELECT a FROM t ORDER BY a", 10))
    assert columns == ["a"]
    assert rows == [{"a": 1}, {"a": 2}]
