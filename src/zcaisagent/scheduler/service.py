"""调度器服务：JobSpec 注册、幂等触发、并发策略、补跑与超时取消。

调度器只产生 Run（通过注入的 RunLauncher），不含业务逻辑、不直接调用 LLM。
"""
from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from zcaisagent.ports.events import EventEnvelope, EventSinkPort
from zcaisagent.ports.scheduler import JobRun, JobSpec
from zcaisagent.ports.security import AuditEvent, AuditPort
from zcaisagent.scheduler.cron import in_timezone, matches, next_after

_ACTIVE_STATUSES = {"queued", "running"}


class RunLauncher(Protocol):
    async def launch(self, job: JobSpec, scheduled_at: datetime) -> str: ...

    async def cancel(self, run_id: str) -> None: ...


class SchedulerService:
    """内存实现的调度服务；JobRun 以 ``job_id:scheduled_at`` 为幂等键。"""

    def __init__(
        self,
        launcher: RunLauncher,
        audit: AuditPort | None = None,
        clock: Callable[[], datetime] | None = None,
        events: EventSinkPort | None = None,
    ) -> None:
        self.launcher = launcher
        self.audit = audit
        self.clock = clock or (lambda: datetime.now(UTC))
        self.events = events
        self._jobs: dict[str, JobSpec] = {}
        self._runs: dict[str, JobRun] = {}

    # —— 存储与审计 ——

    @staticmethod
    def _key(job_id: str, scheduled_at: datetime) -> str:
        return f"{job_id}:{scheduled_at.isoformat()}"

    def _now(self) -> datetime:
        return self.clock()

    def _store(self, run: JobRun) -> None:
        self._runs[self._key(run.job_id, run.scheduled_at)] = run

    def _update(self, run: JobRun, **updates) -> JobRun:
        updated = run.model_copy(update=updates)
        self._store(updated)
        return updated

    def _current(self, run: JobRun) -> JobRun | None:
        return self._runs.get(self._key(run.job_id, run.scheduled_at))

    async def _audit(
        self, action: str, job: JobSpec, run: JobRun | None = None, detail: dict | None = None
    ) -> None:
        if self.audit is None:
            return
        payload = {"job_id": job.job_id, **(detail or {})}
        await self.audit.append(
            AuditEvent(
                action=action,
                actor=job.identity or "scheduler",
                tenant_id=job.tenant_id,
                run_id=run.run_id if run is not None else "",
                detail=payload,
            )
        )

    async def _publish(
        self,
        event_type: str,
        job: JobSpec,
        run: JobRun,
        detail: dict | None = None,
    ) -> None:
        if self.events is None:
            return
        try:
            await self.events.publish(
                EventEnvelope(
                    type=event_type,
                    tenant_id=job.tenant_id,
                    run_id=run.run_id,
                    data={
                        "job_id": job.job_id,
                        "scheduled_at": run.scheduled_at.isoformat(),
                        "status": run.status,
                        **(detail or {}),
                    },
                )
            )
        except Exception:  # noqa: BLE001 - 事件投递失败不阻断调度
            return

    # —— SchedulerPort ——

    async def upsert(self, job: JobSpec) -> str:
        existed = job.job_id in self._jobs
        self._jobs[job.job_id] = job
        await self._audit(
            "job.updated" if existed else "job.created",
            job,
            detail={"name": job.name, "cron": job.cron, "timezone": job.timezone},
        )
        return job.job_id

    async def pause(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is not None:
            self._jobs[job_id] = job.model_copy(update={"enabled": False})

    async def resume(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is not None:
            self._jobs[job_id] = job.model_copy(update={"enabled": True})

    async def delete(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)
        for key in [key for key, run in self._runs.items() if run.job_id == job_id]:
            del self._runs[key]

    async def list_runs(self, job_id: str, limit: int = 20) -> list[JobRun]:
        runs = [run for run in self._runs.values() if run.job_id == job_id]
        runs.sort(key=lambda run: (run.scheduled_at, run.run_id), reverse=True)
        return runs[:limit]

    # —— 触发 ——

    async def tick(self, now: datetime | None = None) -> list[JobRun]:
        """对当前分钟匹配的 enabled job 各触发一次；重复 tick 幂等。"""
        current = now or self._now()
        triggered: list[JobRun] = []
        for job in list(self._jobs.values()):
            if not job.enabled:
                continue
            local = in_timezone(current, job.timezone)
            if not matches(job.cron, local):
                continue
            scheduled_at = local.replace(second=0, microsecond=0)
            triggered.append(await self._trigger(job, scheduled_at))
        return triggered

    async def catch_up(self, job_id: str, now: datetime) -> list[JobRun]:
        """补跑最后一条 JobRun 之后错过的周期；skip 不补，run_once 只补一次。"""
        job = self._jobs.get(job_id)
        if job is None or job.misfire == "skip":
            return []
        runs = [run for run in self._runs.values() if run.job_id == job_id]
        if not runs:
            return []

        latest = max(run.scheduled_at for run in runs)
        cursor = in_timezone(latest, job.timezone).replace(second=0, microsecond=0)
        limit = in_timezone(now, job.timezone)
        missed: list[datetime] = []
        while True:
            nxt = next_after(job.cron, cursor)
            if nxt is None or nxt > limit:
                break
            missed.append(nxt)
            cursor = nxt
            if len(missed) >= 10_000:
                break
        if not missed:
            return []
        return [await self._trigger(job, missed[-1])]

    async def _trigger(self, job: JobSpec, scheduled_at: datetime) -> JobRun:
        key = self._key(job.job_id, scheduled_at)
        existing = self._runs.get(key)
        if existing is not None:
            return existing

        active = sorted(
            (
                run
                for run in self._runs.values()
                if run.job_id == job.job_id and run.status in _ACTIVE_STATUSES
            ),
            key=lambda run: run.scheduled_at,
        )
        if active and job.concurrency == "forbid":
            now = self._now()
            skipped = JobRun(
                job_id=job.job_id,
                run_id=f"skip_{job.job_id}_{uuid.uuid4().hex[:8]}",
                scheduled_at=scheduled_at,
                started_at=now,
                finished_at=now,
                status="skipped",
                error="concurrency_forbidden",
            )
            self._store(skipped)
            await self._audit(
                "job.skipped",
                job,
                skipped,
                {"scheduled_at": scheduled_at.isoformat(), "reason": "concurrency_forbidden"},
            )
            await self._publish(
                "job.skipped",
                job,
                skipped,
                {"reason": "concurrency_forbidden"},
            )
            return skipped

        if active and job.concurrency == "replace":
            for old in active:
                with contextlib.suppress(Exception):
                    await self.launcher.cancel(old.run_id)
                self._update(
                    old,
                    status="failed",
                    error="replaced",
                    finished_at=self._now(),
                )

        run = JobRun(
            job_id=job.job_id,
            run_id=f"job_{job.job_id}_{uuid.uuid4().hex[:8]}",
            scheduled_at=scheduled_at,
            status="queued",
        )
        self._store(run)
        return await self._execute(job, run)

    async def _execute(self, job: JobSpec, run: JobRun) -> JobRun:
        run = self._update(run, status="running", started_at=self._now())
        await self._audit(
            "job.triggered",
            job,
            run,
            {"scheduled_at": run.scheduled_at.isoformat()},
        )
        try:
            run_id = await asyncio.wait_for(
                self.launcher.launch(job, run.scheduled_at), timeout=job.timeout_s
            )
        except TimeoutError:
            with contextlib.suppress(Exception):
                await self.launcher.cancel(run.run_id)
            return await self._finish(job, run, status="failed", error="timeout")
        except Exception as exc:  # noqa: BLE001 - 启动器异常转为失败记录
            return await self._finish(
                job, run, status="failed", error=str(exc) or type(exc).__name__
            )
        return await self._finish(job, run, status="succeeded", run_id=run_id)

    async def _finish(self, job: JobSpec, run: JobRun, **updates) -> JobRun:
        current = self._current(run)
        if current is None or current.status not in _ACTIVE_STATUSES:
            # 已被 replace 标记失败的旧 Run：不覆盖终态。
            return current or run
        finished = self._update(run, finished_at=self._now(), **updates)
        await self._audit(
            "job.succeeded" if finished.status == "succeeded" else "job.failed",
            job,
            finished,
            {"scheduled_at": run.scheduled_at.isoformat(), "error": finished.error},
        )
        if finished.status == "failed":
            await self._publish(
                "job.failed",
                job,
                finished,
                {"error": finished.error},
            )
        return finished
