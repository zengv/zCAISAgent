from zcaisagent.adapters.store.postgres import (
    PsycopgVectorStore,
    schema_sql,
    search_sql,
    upsert_sql,
    vector_literal,
)


class _FakeCursor:
    def __init__(self, rows=None) -> None:
        self.executed: list[tuple[str, object]] = []
        self._rows = rows or []

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def execute(self, sql: str, params=None) -> None:
        self.executed.append((sql, params))

    def executemany(self, sql: str, rows) -> None:
        self.executed.append((sql, list(rows)))

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def commit(self) -> None:
        return None


def test_schema_sql_interpolates_dimensions_and_hnsw() -> None:
    sql = schema_sql(768)

    assert "vector(768)" in sql
    assert "hnsw" in sql
    assert "vector_cosine_ops" in sql


def test_search_sql_filters_tenant_acl_and_orders_by_distance() -> None:
    sql = search_sql()

    assert "tenant_id = %s" in sql
    assert "acl && %s::text[]" in sql
    assert "<=> %s::vector" in sql
    assert "ORDER BY distance" in sql


def test_upsert_sql_uses_conflict_update() -> None:
    sql = upsert_sql()

    assert "ON CONFLICT (id) DO UPDATE" in sql
    assert "%s::vector" in sql


def test_vector_literal_formats_floats() -> None:
    assert vector_literal([0.1, 1.0]) == "[0.100000,1.000000]"


def test_search_passes_bounded_params() -> None:
    cursor = _FakeCursor()
    store = PsycopgVectorStore("dsn", 4)
    store._conn = _FakeConn(cursor)

    store.search([0.5, 0.5, 0.5, 0.5], "demo", ["public"], 2)

    sql, params = cursor.executed[0]
    assert "tenant_id = %s" in sql
    assert params[1] == "demo"
    assert params[2] == ["public"]
    # 版本过滤在 Python 侧完成，SQL 多取 3 倍候选。
    assert params[3] == 6
