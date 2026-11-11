"""可选 Postgres checkpoint 适配器：历史 append + 最新快照 upsert。"""
from __future__ import annotations

import json
from typing import Any

from zcaisagent.domain.models import RunState


class CheckpointUnavailable(Exception):
    """连接/驱动不可用；调用方应回退 JSONL。"""


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS run_checkpoints ("
    "run_id text NOT NULL, seq bigint NOT NULL, state jsonb NOT NULL, "
    "PRIMARY KEY (run_id, seq));"
    "CREATE TABLE IF NOT EXISTS run_latest ("
    "run_id text PRIMARY KEY, thread_id text, tenant_id text, user_id text, "
    "status text, created_ts double precision, finished_ts double precision, "
    "state jsonb NOT NULL);"
    "CREATE INDEX IF NOT EXISTS run_latest_tenant_idx ON run_latest (tenant_id);"
)


class PostgresCheckpoint:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._conn: Any = None

    def _connect(self):
        if self._conn is not None:
            return self._conn
        if not self.dsn:
            raise CheckpointUnavailable("database dsn is empty")
        try:
            import psycopg
        except ImportError as exc:
            raise CheckpointUnavailable("psycopg is not installed") from exc
        try:
            self._conn = psycopg.connect(self.dsn)
        except Exception as exc:  # noqa: BLE001
            raise CheckpointUnavailable(str(exc)) from exc
        return self._conn

    def ensure_schema(self) -> None:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(_SCHEMA)
        conn.commit()

    async def save(self, state: RunState) -> None:
        conn = self._connect()
        payload = state.model_dump_json()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM run_checkpoints WHERE run_id=%s",
                (state.run_id,),
            )
            seq = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO run_checkpoints (run_id, seq, state) VALUES (%s,%s,%s::jsonb)",
                (state.run_id, seq, payload),
            )
            cur.execute(
                "INSERT INTO run_latest "
                "(run_id,thread_id,tenant_id,user_id,status,created_ts,finished_ts,state) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb) "
                "ON CONFLICT (run_id) DO UPDATE SET "
                "thread_id=EXCLUDED.thread_id,tenant_id=EXCLUDED.tenant_id,"
                "user_id=EXCLUDED.user_id,status=EXCLUDED.status,"
                "created_ts=EXCLUDED.created_ts,finished_ts=EXCLUDED.finished_ts,"
                "state=EXCLUDED.state",
                (
                    state.run_id,
                    state.thread_id,
                    state.tenant_id,
                    state.user_id,
                    state.status.value,
                    state.created_ts,
                    state.finished_ts,
                    payload,
                ),
            )
        conn.commit()

    @staticmethod
    def _state(row: Any) -> RunState:
        value = row[0]
        return RunState.model_validate_json(
            value if isinstance(value, str) else json.dumps(value)
        )

    async def load(self, run_id: str) -> RunState | None:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute("SELECT state FROM run_latest WHERE run_id=%s", (run_id,))
            row = cur.fetchone()
        return None if row is None else self._state(row)

    async def list_thread(self, thread_id: str) -> list[RunState]:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM run_latest WHERE thread_id=%s "
                "ORDER BY created_ts DESC",
                (thread_id,),
            )
            rows = cur.fetchall()
        return [self._state(row) for row in rows]

    async def list_runs(self, limit: int = 50) -> list[RunState]:
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM run_latest ORDER BY created_ts DESC LIMIT %s",
                (max(0, int(limit)),),
            )
            rows = cur.fetchall()
        return [self._state(row) for row in rows]
