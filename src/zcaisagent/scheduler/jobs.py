"""定时任务装配：从 TOML 加载 JobSpec，并用注入回调触发 Run。"""
from __future__ import annotations

import tomllib
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from zcaisagent.ports.scheduler import JobSpec

RunGoal = Callable[[str, str, str, str], Awaitable[str]]


class ContainerRunLauncher:
    """把 JobSpec 转成一次 Run；执行细节由装配层注入的 ``run_goal`` 提供。"""

    def __init__(self, run_goal: RunGoal) -> None:
        self.run_goal = run_goal

    async def launch(self, job: JobSpec, scheduled_at: datetime) -> str:
        goal = str(job.input.get("goal") or job.name)
        level = str(job.input.get("level") or "L1")
        user_id = job.identity or "scheduler"
        return await self.run_goal(goal, job.tenant_id, user_id, level)

    async def cancel(self, run_id: str) -> None:
        cancel = getattr(self.run_goal, "cancel", None)
        if callable(cancel):
            await cancel(run_id)


def _job_from(name: str, body: dict) -> JobSpec | None:
    data = dict(body)
    job_id = str(data.get("job_id") or name)
    if not job_id:
        return None
    data["job_id"] = job_id
    data.setdefault("name", job_id)
    data.setdefault("tenant_id", "demo")
    try:
        return JobSpec.model_validate(data)
    except ValidationError:
        return None


def load_jobs(path: str | Path) -> list[JobSpec]:
    """读取 ``[jobs.<id>]`` 或 ``[[jobs]]``；缺字段用默认值，非法条目跳过。"""
    file = Path(path)
    if not file.is_file():
        return []
    with file.open("rb") as handle:
        data = tomllib.load(handle)
    raw = data.get("jobs", [])
    jobs: list[JobSpec] = []
    if isinstance(raw, dict):
        for name, body in raw.items():
            if isinstance(body, dict):
                job = _job_from(str(name), body)
                if job is not None:
                    jobs.append(job)
    elif isinstance(raw, list):
        for body in raw:
            if isinstance(body, dict):
                job = _job_from("", body)
                if job is not None:
                    jobs.append(job)
    return jobs
