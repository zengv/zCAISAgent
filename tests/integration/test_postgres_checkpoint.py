import asyncio
import os

import pytest

from zcaisagent.adapters.checkpoint.postgres import PostgresCheckpoint
from zcaisagent.domain.models import RunState

DSN = os.getenv("ZCAISAGENT_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="ZCAISAGENT_TEST_DATABASE_URL not set"
)


def _state(run_id: str, created: float) -> RunState:
    return RunState(
        run_id=run_id,
        thread_id="t1",
        tenant_id="demo",
        user_id="u",
        goal=f"q-{run_id}",
        created_ts=created,
        finished_ts=created + 1,
    )


def test_postgres_checkpoint_roundtrip() -> None:
    cp = PostgresCheckpoint(DSN)
    cp.ensure_schema()
    asyncio.run(cp.save(_state("pg_run_a", 1.0)))
    asyncio.run(cp.save(_state("pg_run_a", 2.0)))
    loaded = asyncio.run(cp.load("pg_run_a"))
    assert loaded is not None and loaded.created_ts == 2.0
    runs = {r.run_id for r in asyncio.run(cp.list_runs())}
    assert "pg_run_a" in runs
