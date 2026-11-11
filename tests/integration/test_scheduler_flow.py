import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from zcaisagent.cli import build_scheduler_launcher
from zcaisagent.container import build_container
from zcaisagent.scheduler.jobs import load_jobs
from zcaisagent.scheduler.service import SchedulerService
from zcaisagent.settings import Settings


def test_daily_research_scheduler_flow(tmp_path: Path) -> None:
    jobs = {job.job_id: job for job in load_jobs(Path("configs/schedules.toml"))}
    job = jobs["daily_research"].model_copy(update={"enabled": True})
    container = build_container(
        Settings(data_dir=tmp_path, seed_dir=Path("data/seed"))
    )
    launcher = build_scheduler_launcher(container)

    async def scenario():
        service = SchedulerService(launcher, audit=container.audit)
        await service.upsert(job)
        now = datetime(2026, 11, 9, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        triggered = await service.tick(now)
        listed = await service.list_runs(job.job_id)
        return triggered, listed

    triggered, listed = asyncio.run(scenario())

    assert len(triggered) == 1
    assert triggered[0].status == "succeeded"
    assert triggered[0].run_id == listed[0].run_id

    state = asyncio.run(container.checkpoint.load(triggered[0].run_id))
    assert state is not None
    assert state.goal == job.input["goal"]
