"""把配置目录中的 CSV/JSON/Excel 加载为 SQLite 内存表，供只读 SQL 查询。"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import sqlite3
import threading
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from decimal import Decimal
from pathlib import Path
from typing import Any

from zcaisagent.capabilities.n2sql.dialects import quote_identifier

DEFAULT_EXTENSIONS = (".csv", ".json", ".xlsx", ".xlsm")
DEFAULT_MAX_ROWS = 20000
DEFAULT_MAX_BYTES = 20 * 1024 * 1024


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


class FileSourceUnavailable(RuntimeError):
    """目录不存在或没有可加载的数据文件。"""


def _unique_headers(headers: list[Any]) -> list[str]:
    counts: dict[str, int] = {}
    result: list[str] = []
    for index, raw in enumerate(headers):
        name = str(raw).strip() if raw is not None else ""
        name = name or f"col_{index + 1}"
        if name in counts:
            counts[name] += 1
            name = f"{name}_{counts[name]}"
        else:
            counts[name] = 0
        result.append(name)
    return result


def _is_int_text(text: str) -> bool:
    return bool(re.fullmatch(r"[+-]?\d+", text))


def _column_type(values: list[Any]) -> str:
    has_value = False
    all_int = True
    for value in values:
        if value is None or value == "":
            continue
        has_value = True
        if isinstance(value, bool):
            return "TEXT"
        if isinstance(value, int):
            continue
        if isinstance(value, float):
            all_int = False
            continue
        text = str(value).strip()
        try:
            float(text)
        except ValueError:
            return "TEXT"
        if not _is_int_text(text):
            all_int = False
    if not has_value:
        return "TEXT"
    return "INTEGER" if all_int else "REAL"


def _convert(value: Any, column_type: str) -> Any:
    if value is None or value == "":
        return None
    if column_type == "INTEGER":
        try:
            return int(str(value).strip())
        except ValueError:
            return None
    if column_type == "REAL":
        try:
            return float(str(value).strip())
        except ValueError:
            return None
    return str(value)


def _read_csv(path: Path, max_rows: int) -> tuple[list[str], list[list[Any]]]:
    text: str | None = None
    for encoding in ("utf-8-sig", "gbk"):
        try:
            text = path.read_text(encoding=encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("无法识别文件编码")
    reader = csv.reader(io.StringIO(text))
    raw_headers = next(reader, [])
    headers = _unique_headers(raw_headers)
    rows: list[list[Any]] = []
    for row in reader:
        if len(rows) >= max_rows:
            break
        rows.append(list(row[: len(headers)]) + [None] * max(0, len(headers) - len(row)))
    return headers, rows


def _read_json(path: Path, max_rows: int) -> tuple[list[str], list[list[Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict):
        for key in ("rows", "data", "items", "records"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list) or not payload:
        raise ValueError("JSON 需要是非空对象数组")
    records = [item for item in payload if isinstance(item, dict)]
    if not records:
        raise ValueError("JSON 数组元素需要是对象")
    headers: list[str] = []
    for item in records:
        for key in item:
            if key not in headers:
                headers.append(str(key))
    headers = _unique_headers(headers)
    rows = [
        [item.get(header) for header in headers] for item in records[:max_rows]
    ]
    return headers, rows


def _read_excel(path: Path, max_rows: int) -> tuple[list[str], list[list[Any]]]:
    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        iterator = sheet.iter_rows(values_only=True)
        raw_headers = next(iterator, None) or ()
        headers = _unique_headers(list(raw_headers))
        rows: list[list[Any]] = []
        for row in iterator:
            if len(rows) >= max_rows:
                break
            rows.append(list(row[: len(headers)]) + [None] * max(0, len(headers) - len(row)))
        return headers, rows
    finally:
        workbook.close()


_READERS = {
    ".csv": _read_csv,
    ".json": _read_json,
    ".xlsx": _read_excel,
    ".xlsm": _read_excel,
}


def _table_name(path: Path, used: set[str]) -> str:
    base = re.sub(r"[^0-9A-Za-z_]+", "_", path.stem).strip("_").lower()
    if not base:
        base = "data"
    elif base[0].isdigit():
        base = f"data_{base}"
    name = base
    index = 2
    while name in used:
        name = f"{base}_{index}"
        index += 1
    used.add(name)
    return name


class FileTableExecutor:
    """启动时把目录内文件装入 SQLite 内存库；之后只允许只读查询。"""

    def __init__(
        self,
        directory: str | Path,
        *,
        extensions: tuple[str, ...] = DEFAULT_EXTENSIONS,
        max_rows: int = DEFAULT_MAX_ROWS,
        max_file_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        root = Path(directory).expanduser()
        if not root.is_dir():
            raise FileSourceUnavailable(f"file source directory not found: {directory}")
        self._root = root
        self._max_rows = max(1, int(max_rows))
        self._max_file_bytes = max(1024, int(max_file_bytes))
        self._extensions = {item.lower() for item in extensions}
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(
            ":memory:", check_same_thread=False, isolation_level=None
        )
        self.warnings: list[str] = []
        self.tables: list[str] = []
        parts: list[str] = []
        self._load(parts)
        if not self.tables:
            self._connection.close()
            raise FileSourceUnavailable(f"no data file loaded from: {directory}")
        self._connection.execute("PRAGMA query_only = ON")
        self._schema = "\n".join(parts)

    @property
    def schema(self) -> str:
        return self._schema

    def _load(self, parts: list[str]) -> None:
        used: set[str] = set()
        for path in sorted(self._root.iterdir()):
            if not path.is_file() or path.suffix.lower() not in self._extensions:
                continue
            try:
                size = path.stat().st_size
            except OSError as exc:
                self.warnings.append(f"{path.name}: {exc}")
                continue
            if size > self._max_file_bytes:
                self.warnings.append(f"{path.name}: 超过文件大小上限")
                continue
            reader = _READERS.get(path.suffix.lower())
            if reader is None:
                continue
            try:
                headers, rows = reader(path, self._max_rows)
            except Exception as exc:  # noqa: BLE001 - 单个文件失败不阻断其他文件
                self.warnings.append(f"{path.name}: {exc}")
                continue
            if not headers or not rows:
                self.warnings.append(f"{path.name}: 没有可加载的数据")
                continue
            table = _table_name(path, used)
            self._create_table(table, headers, rows)
            columns = ", ".join(
                f"{quote_identifier('sqlite', header)} {_column_type([row[index] for row in rows])}"
                for index, header in enumerate(headers)
            )
            parts.append(
                f"-- 文件：{path.name}（{len(rows)} 行）\n"
                f"CREATE TABLE {quote_identifier('sqlite', table)} ({columns});"
            )
            self.tables.append(table)

    def _create_table(self, table: str, headers: list[str], rows: list[list[Any]]) -> None:
        types = [
            _column_type([row[index] for row in rows]) for index in range(len(headers))
        ]
        columns = ", ".join(
            f"{quote_identifier('sqlite', header)} {types[index]}"
            for index, header in enumerate(headers)
        )
        self._connection.execute(
            f"CREATE TABLE {quote_identifier('sqlite', table)} ({columns})"
        )
        placeholders = ", ".join("?" for _ in headers)
        values = [
            [_convert(value, types[index]) for index, value in enumerate(row)]
            for row in rows
        ]
        self._connection.executemany(
            f"INSERT INTO {quote_identifier('sqlite', table)} VALUES ({placeholders})",
            values,
        )

    async def execute(
        self, sql: str, max_rows: int
    ) -> tuple[list[str], list[dict[str, Any]]]:
        return await asyncio.to_thread(self._execute, sql, max_rows)

    def _execute(self, sql: str, max_rows: int) -> tuple[list[str], list[dict[str, Any]]]:
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute(sql)
                rows = cursor.fetchmany(max(1, int(max_rows)))
                columns = [item[0] for item in cursor.description or []]
                return columns, [
                    {column: _jsonable(value) for column, value in zip(columns, row, strict=False)}
                    for row in rows
                ]
            finally:
                cursor.close()
