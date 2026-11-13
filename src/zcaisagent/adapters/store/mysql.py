from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from decimal import Decimal
from typing import Any

try:  # 驱动是可选的：缺失时由容器降级，不阻断服务启动。
    import pymysql  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - 仅在未安装可选依赖时触发
    pymysql = None


class MySqlUnavailable(RuntimeError):
    """驱动缺失或连接不可用。"""


def mysql_available() -> bool:
    return pymysql is not None


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (date, dt_time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


class MySqlReadonlyClient:
    """单次查询新建连接，读取上限由 fetchmany 控制；阻塞驱动放到线程池执行。"""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        timeout_s: float = 5.0,
    ) -> None:
        if pymysql is None:
            raise MySqlUnavailable("pymysql is not installed")
        self._host = host
        self._port = int(port)
        self._user = user
        self._password = password
        self._database = database
        self._timeout_s = max(1.0, float(timeout_s))

    async def execute(
        self, sql: str, max_rows: int
    ) -> tuple[list[str], list[dict[str, Any]]]:
        return await asyncio.to_thread(self._execute, sql, max_rows)

    def _execute(self, sql: str, max_rows: int) -> tuple[list[str], list[dict[str, Any]]]:
        connection = pymysql.connect(
            host=self._host,
            port=self._port,
            user=self._user,
            password=self._password,
            database=self._database,
            charset="utf8mb4",
            connect_timeout=self._timeout_s,
            read_timeout=self._timeout_s,
            write_timeout=self._timeout_s,
            autocommit=True,
        )
        try:
            with connection.cursor() as cursor:
                # 会话级保护：限制 SELECT 执行时间；权限不足时忽略。
                try:
                    cursor.execute(
                        f"SET SESSION MAX_EXECUTION_TIME={int(self._timeout_s * 1000)}"
                    )
                except Exception:  # noqa: BLE001 - 保护性设置失败不影响查询
                    pass
                cursor.execute(sql)
                rows = cursor.fetchmany(max(1, int(max_rows)))
                columns = [item[0] for item in cursor.description or []]
                return columns, [
                    {column: _jsonable(value) for column, value in zip(columns, row, strict=False)}
                    for row in rows
                ]
        finally:
            connection.close()
