from __future__ import annotations

from typing import Any

from zcaisagent.adapters.store.result import DbApiReadonlyClient

try:  # 驱动是可选依赖：缺失时由容器降级，不阻断服务启动。
    import psycopg  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - 仅在未安装可选依赖时触发
    psycopg = None


class PostgresUnavailable(RuntimeError):
    """驱动缺失或连接不可用。"""


def postgres_available() -> bool:
    return psycopg is not None


class PostgresReadonlyClient(DbApiReadonlyClient):
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
        if psycopg is None:
            raise PostgresUnavailable("psycopg is not installed")
        timeout_ms = int(max(1.0, float(timeout_s)) * 1000)

        def connect() -> Any:
            return psycopg.connect(
                host=host,
                port=int(port),
                user=user,
                password=password,
                dbname=database,
                connect_timeout=int(max(1.0, float(timeout_s))),
                autocommit=True,
                options=(
                    f"-c statement_timeout={timeout_ms}"
                    " -c default_transaction_read_only=on"
                ),
            )

        super().__init__(connect, f"postgres {host}:{port}/{database}")
