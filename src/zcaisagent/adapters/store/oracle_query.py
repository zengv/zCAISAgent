from __future__ import annotations

from typing import Any

from zcaisagent.adapters.store.result import DbApiReadonlyClient

try:  # 驱动是可选依赖：缺失时由容器降级，不阻断服务启动。
    import oracledb  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - 仅在未安装可选依赖时触发
    oracledb = None


class OracleUnavailable(RuntimeError):
    """驱动缺失或连接不可用。"""


def oracle_available() -> bool:
    return oracledb is not None


class OracleReadonlyClient(DbApiReadonlyClient):
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
        if oracledb is None:
            raise OracleUnavailable("oracledb is not installed")
        dsn = f"{host}:{int(port)}/{database}"

        def connect() -> Any:
            connection = oracledb.connect(user=user, password=password, dsn=dsn)
            connection.call_timeout = int(max(1.0, float(timeout_s)) * 1000)
            cursor = connection.cursor()
            try:
                cursor.execute("SET TRANSACTION READ ONLY")
            finally:
                cursor.close()
            return connection

        super().__init__(connect, f"oracle {host}:{port}/{database}")
