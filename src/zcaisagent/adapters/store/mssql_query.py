from __future__ import annotations

from typing import Any

from zcaisagent.adapters.store.result import DbApiReadonlyClient

try:  # 驱动是可选依赖：缺失时由容器降级，不阻断服务启动。
    import pyodbc  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - 仅在未安装可选依赖时触发
    pyodbc = None

DEFAULT_DRIVER = "ODBC Driver 18 for SQL Server"


class MsSqlUnavailable(RuntimeError):
    """驱动缺失或连接不可用。"""


def mssql_available() -> bool:
    return pyodbc is not None


class MsSqlReadonlyClient(DbApiReadonlyClient):
    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        timeout_s: float = 5.0,
        driver: str = "",
    ) -> None:
        if pyodbc is None:
            raise MsSqlUnavailable("pyodbc is not installed")
        timeout = int(max(1.0, float(timeout_s)))
        connection_string = (
            f"DRIVER={{{driver or DEFAULT_DRIVER}}};"
            f"SERVER={host},{int(port)};DATABASE={database};"
            f"UID={user};PWD={password};Encrypt=yes;TrustServerCertificate=yes;"
            f"Connection Timeout={timeout}"
        )

        def connect() -> Any:
            connection = pyodbc.connect(
                connection_string, timeout=timeout, autocommit=True
            )
            connection.timeout = timeout
            return connection

        super().__init__(connect, f"mssql {host}:{port}/{database}")
