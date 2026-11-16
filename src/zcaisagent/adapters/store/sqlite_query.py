from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

from zcaisagent.adapters.store.result import DbApiReadonlyClient


class SqliteUnavailable(RuntimeError):
    """数据库文件不存在或不可读。"""


class SqliteReadonlyClient(DbApiReadonlyClient):
    def __init__(self, *, database: str, timeout_s: float = 5.0) -> None:
        path = Path(database).expanduser()
        if not path.is_file():
            raise SqliteUnavailable(f"sqlite database not found: {database}")
        uri_path = quote(path.resolve().as_posix(), safe="/:")
        uri = f"file:{uri_path}?mode=ro"

        def connect() -> Any:
            connection = sqlite3.connect(
                uri,
                uri=True,
                check_same_thread=False,
                timeout=max(1.0, float(timeout_s)),
            )
            connection.execute("PRAGMA query_only = ON")
            return connection

        super().__init__(connect, f"sqlite {path.name}")
