import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from zcaisagent.ports.scheduler import JobSpec
from zcaisagent.ports.security import AuditEvent
from zcaisagent.scheduler.cron import in_timezone, matches, next_after, parse
from zcaisagent.scheduler.jobs import load_jobs
from zcaisagent.scheduler.service import SchedulerService


def _job(**overrides) -> JobSpec:
    data = {
        "job_id": "j1",
        "tenant_id": "demo",
        "name": "测试任务",
        "cron": "* * * * *",
        "timezone": "UTC",
    }
    data.update(overrides)
    return JobSpec(**data)


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def append(self, event: AuditEvent) -> None:
        self.events.append(event)

    @property
    def actions(self) -> list[str]:
        return [event.action for event in self.events]


class RecordingLauncher:
    def __init__(self) -> None:
        self.launched: list[tuple[str, datetime]] = []
        self.cancelled: list[str] = []
        self.calls = 0

    async def launch(self, job: JobSpec, scheduled_at: datetime) -> str:
        self.calls += 1
        self.launched.append((job.job_id, scheduled_at))
        return f"run_{self.calls}"

    async def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)


class GatedLauncher:
    """首次 launch 挂起，用于模拟仍在运行的上一条 JobRun。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled: list[str] = []
        self.calls = 0

    async def launch(self, job: JobSpec, scheduled_at: datetime) -> str:
        self.calls += 1
        index = self.calls
        if index == 1:
            self.started.set()
            await self.release.wait()
        return f"run_{index}"

    async def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)


class SlowLauncher:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    async def launch(self, job: JobSpec, scheduled_at: datetime) -> str:
        await asyncio.sleep(0.2)
        return "run_slow"

    async def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)


# —— cron ——


def test_cron_step_and_weekday() -> None:
    assert matches("*/5 * * * *", datetime(2026, 11, 9, 8, 10, tzinfo=UTC))
    assert not matches("*/5 * * * *", datetime(2026, 11, 9, 8, 11, tzinfo=UTC))

    monday = datetime(2026, 11, 9, 8, 0, tzinfo=UTC)
    saturday = datetime(2026, 11, 14, 8, 0, tzinfo=UTC)
    assert matches("0 8 * * 1-5", monday)
    assert not matches("0 8 * * 1-5", saturday)
    assert not matches("0 8 * * 1-5", monday.replace(minute=1))


def test_cron_monthly_and_sunday_alias() -> None:
    assert matches("0 0 1 * *", datetime(2026, 12, 1, 0, 0, tzinfo=UTC))
    assert not matches("0 0 1 * *", datetime(2026, 12, 2, 0, 0, tzinfo=UTC))

    sunday = datetime(2026, 11, 15, 8, 0, tzinfo=UTC)
    assert matches("0 8 * * 0", sunday)
    assert matches("0 8 * * 7", sunday)


def test_cron_dom_and_dow_use_or_semantics() -> None:
    friday = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)
    thirteenth = datetime(2026, 12, 13, 0, 0, tzinfo=UTC)
    assert matches("0 0 13 * 5", friday)
    assert matches("0 0 13 * 5", thirteenth)
    assert not matches("0 0 13 * 5", datetime(2026, 11, 12, 0, 0, tzinfo=UTC))


@pytest.mark.parametrize(
    "expr",
    ["* * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *", "* * * * 8", "a b c d e"],
)
def test_cron_invalid_expression(expr: str) -> None:
    with pytest.raises(ValueError):
        parse(expr)


def test_timezone_conversion_and_fallback() -> None:
    utc = datetime(2026, 11, 11, 0, 0, tzinfo=UTC)
    shanghai = in_timezone(utc, "Asia/Shanghai")
    assert shanghai.hour == 8
    assert shanghai.tzinfo == ZoneInfo("Asia/Shanghai")

    fallback = in_timezone(utc, "Not/AZone")
    assert fallback.utcoffset() == timedelta(0)


def test_next_after_is_strictly_later() -> None:
    monday = datetime(2026, 11, 9, 7, 59, tzinfo=UTC)
    assert next_after("0 8 * * 1-5", monday) == datetime(
        2026, 11, 9, 8, 0, tzinfo=UTC
    )
    after_exact = datetime(2026, 11, 9, 8, 0, tzinfo=UTC)
    assert next_after("0 8 * * 1-5", after_exact) == datetime(
        2026, 11, 10, 8, 0, tzinfo=UTC
    )


def test_load_jobs_defaults_and_missing_fields(tmp_path: Path) -> None:
    path = tmp_path / "jobs.toml"
    path.write_text('[jobs.alpha]\ncron = "* * * * *"\n', encoding="utf-8")
    jobs = load_jobs(path)

    assert len(jobs) == 1
    assert jobs[0].job_id == "alpha"
    assert jobs[0].name == "alpha"
    assert jobs[0].tenant_id == "demo"


def test_daily_research_config_is_disabled_by_default() -> None:
    jobs = {job.job_id: job for job in load_jobs(Path("configs/schedules.toml"))}
    job = jobs["daily_research"]

    assert job.enabled is False
    assert job.cron == "0 8 * * 1-5"
    assert job.timezone == "Asia/Shanghai"
    assert job.input["goal"]


# —— service ——


def test_tick_is_idempotent_and_audits() -> None:
    async def scenario():
        launcher = RecordingLauncher()
        audit = FakeAudit()
        service = SchedulerService(launcher, audit=audit)
        await service.upsert(_job())
        now = datetime(2026, 11, 9, 8, 0, tzinfo=UTC)
        first = await service.tick(now)
        second = await service.tick(now)
        runs = await service.list_runs("j1")
        return first, second, runs, launcher, audit

    first, second, runs, launcher, audit = asyncio.run(scenario())

    assert len(launcher.launched) == 1
    assert len(runs) == 1
    assert first[0].status == "succeeded"
    assert second[0].run_id == first[0].run_id
    assert first[0].scheduled_at == datetime(2026, 11, 9, 8, 0, tzinfo=UTC)
    assert audit.actions[:2] == ["job.created", "job.triggered"]
    assert "job.succeeded" in audit.actions


def test_concurrency_forbid_marks_skipped() -> None:
    async def scenario():
        launcher = GatedLauncher()
        audit = FakeAudit()
        service = SchedulerService(launcher, audit=audit)
        await service.upsert(_job(concurrency="forbid"))
        start = datetime(2026, 11, 9, 8, 0, tzinfo=UTC)

        first_task = asyncio.create_task(service.tick(start))
        await asyncio.wait_for(launcher.started.wait(), timeout=1)
        skipped = await service.tick(start + timedelta(minutes=1))
        launcher.release.set()
        first = await first_task
        runs = await service.list_runs("j1")
        return first, skipped, runs, launcher, audit

    first, skipped, runs, launcher, audit = asyncio.run(scenario())

    assert first[0].status == "succeeded"
    assert skipped[0].status == "skipped"
    assert skipped[0].error == "concurrency_forbidden"
    assert launcher.calls == 1
    assert len(runs) == 2
    assert "job.skipped" in audit.actions


def test_concurrency_replace_cancels_previous_run() -> None:
    async def scenario():
        launcher = GatedLauncher()
        service = SchedulerService(launcher)
        await service.upsert(_job(concurrency="replace"))
        start = datetime(2026, 11, 9, 8, 0, tzinfo=UTC)

        first_task = asyncio.create_task(service.tick(start))
        await asyncio.wait_for(launcher.started.wait(), timeout=1)
        second = await service.tick(start + timedelta(minutes=1))
        launcher.release.set()
        first = await first_task
        runs = await service.list_runs("j1")
        return first, second, runs, launcher

    first, second, runs, launcher = asyncio.run(scenario())

    assert second[0].status == "succeeded"
    assert first[0].status == "failed"
    assert first[0].error == "replaced"
    assert launcher.cancelled
    assert len(runs) == 2


def test_concurrency_allow_runs_in_parallel() -> None:
    async def scenario():
        launcher = GatedLauncher()
        service = SchedulerService(launcher)
        await service.upsert(_job(concurrency="allow"))
        start = datetime(2026, 11, 9, 8, 0, tzinfo=UTC)

        first_task = asyncio.create_task(service.tick(start))
        await asyncio.wait_for(launcher.started.wait(), timeout=1)
        second = await service.tick(start + timedelta(minutes=1))
        launcher.release.set()
        first = await first_task
        runs = await service.list_runs("j1")
        return first, second, runs, launcher

    first, second, runs, launcher = asyncio.run(scenario())

    assert launcher.calls == 2
    assert first[0].status == "succeeded"
    assert second[0].status == "succeeded"
    assert [run.status for run in runs] == ["succeeded", "succeeded"]


def test_misfire_run_once_only_catches_up_last() -> None:
    async def scenario():
        launcher = RecordingLauncher()
        service = SchedulerService(launcher)
        await service.upsert(_job(cron="0 * * * *", misfire="run_once"))
        await service.tick(datetime(2026, 11, 9, 0, 0, tzinfo=UTC))
        caught = await service.catch_up("j1", datetime(2026, 11, 9, 3, 0, tzinfo=UTC))
        runs = await service.list_runs("j1")
        return caught, runs, launcher

    caught, runs, launcher = asyncio.run(scenario())

    assert len(caught) == 1
    assert caught[0].scheduled_at == datetime(2026, 11, 9, 3, 0, tzinfo=UTC)
    assert len(runs) == 2
    assert len(launcher.launched) == 2


def test_misfire_skip_does_not_catch_up() -> None:
    async def scenario():
        launcher = RecordingLauncher()
        service = SchedulerService(launcher)
        await service.upsert(_job(cron="0 * * * *", misfire="skip"))
        await service.tick(datetime(2026, 11, 9, 0, 0, tzinfo=UTC))
        caught = await service.catch_up("j1", datetime(2026, 11, 9, 3, 0, tzinfo=UTC))
        runs = await service.list_runs("j1")
        return caught, runs

    caught, runs = asyncio.run(scenario())

    assert caught == []
    assert len(runs) == 1


def test_timeout_marks_failed_and_cancels() -> None:
    async def scenario():
        launcher = SlowLauncher()
        audit = FakeAudit()
        service = SchedulerService(launcher, audit=audit)
        await service.upsert(_job(timeout_s=0))
        runs = await service.tick(datetime(2026, 11, 9, 8, 0, tzinfo=UTC))
        return runs, launcher, audit

    runs, launcher, audit = asyncio.run(scenario())

    assert runs[0].status == "failed"
    assert runs[0].error == "timeout"
    assert launcher.cancelled
    assert "job.failed" in audit.actions
