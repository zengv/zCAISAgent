"""数据库只读执行器的公共部分：线程池执行与结果 JSON 化。"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from decimal import Decimal
from typing import Any

ConnectionFactory = Callable[[], Any]


def jsonable(value: Any) -> Any:
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


class DbApiReadonlyClient:
    """单次查询新建连接，行数由 fetchmany 截断；阻塞驱动放到线程池执行。"""

    def __init__(self, connect: ConnectionFactory, label: str) -> None:
        self._connect = connect
        self._label = label

    @property
    def label(self) -> str:
        return self._label

    async def execute(
        self, sql: str, max_rows: int
    ) -> tuple[list[str], list[dict[str, Any]]]:
        return await asyncio.to_thread(self._execute, sql, max_rows)

    def _execute(self, sql: str, max_rows: int) -> tuple[list[str], list[dict[str, Any]]]:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(sql)
                rows = cursor.fetchmany(max(1, int(max_rows)))
                columns = [item[0] for item in cursor.description or []]
                return columns, [
                    {column: jsonable(value) for column, value in zip(columns, row, strict=False)}
                    for row in rows
                ]
            finally:
                cursor.close()
        finally:
            connection.close()
