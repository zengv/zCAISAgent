from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, Field


class NotifyTarget(BaseModel):
    kind: Literal["inbox", "webhook", "email"]
    target: str


class JobSpec(BaseModel):
    """定时任务定义；调度器只按 cron 触发 Run，不含业务逻辑。"""

    job_id: str
    tenant_id: str
    name: str
    cron: str  # 5 段或 6 段（含秒）；如 "0 8 * * 1-5"
    timezone: str = "Asia/Shanghai"
    agent: str = ""
    input: dict = Field(default_factory=dict)
    identity: str = ""
    enabled: bool = True
    concurrency: Literal["forbid", "replace", "allow"] = "forbid"
    misfire: Literal["skip", "run_once"] = "run_once"
    timeout_s: int = 300
    budget_profile: str = "scheduled"
    notify: list[NotifyTarget] = Field(default_factory=list)
    schema_version: int = 1


class JobRun(BaseModel):
    job_id: str
    run_id: str
    scheduled_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: Literal["queued", "running", "succeeded", "failed", "skipped"] = "queued"
    error: str | None = None


class SchedulerPort(Protocol):
    async def upsert(self, job: JobSpec) -> str: ...

    async def pause(self, job_id: str) -> None: ...

    async def resume(self, job_id: str) -> None: ...

    async def delete(self, job_id: str) -> None: ...

    async def list_runs(self, job_id: str, limit: int = 20) -> list[JobRun]: ...
