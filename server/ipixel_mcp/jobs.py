"""Async job registry for long BLE media transfers (review C-2).

A ``display_image``/animation transfer can take 20-90 s — well past the Worker
~100 s fetch ceiling and MCP client timeouts. So the tool call enqueues a job,
returns a ``job_id`` immediately, and the model polls status. The registry is
in-process (jobs don't survive restart — they're transient transfers, not the
notification queue) and hardware-free: the worker coroutine is injected.

Stdlib-only; ``asyncio`` + an injectable clock for deterministic tests.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("ipixel_mcp.jobs")

# Job lifecycle states.
QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"

_TERMINAL = frozenset({SUCCEEDED, FAILED})


@dataclass
class Job:
    id: str
    kind: str
    status: str = QUEUED
    result: Any = None
    error: Optional[str] = None
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "kind": self.kind,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# A coroutine that performs the actual work. Result is stored on success; any
# exception is captured into ``error`` (generic message; detail is logged).
JobWork = Callable[[], Awaitable[Any]]


class JobRegistry:
    """Tracks media-transfer jobs and runs their work as background tasks."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_jobs: int = 256,
    ) -> None:
        self._clock = clock
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._max_jobs = max_jobs

    def _new_id(self) -> str:
        return uuid.uuid4().hex

    def _gc(self) -> None:
        # Drop oldest terminal jobs if we exceed the cap.
        if len(self._jobs) <= self._max_jobs:
            return
        terminal = sorted(
            (j for j in self._jobs.values() if j.status in _TERMINAL),
            key=lambda j: j.updated_at,
        )
        while len(self._jobs) > self._max_jobs and terminal:
            victim = terminal.pop(0)
            self._jobs.pop(victim.id, None)
            self._tasks.pop(victim.id, None)

    def submit(self, kind: str, work: JobWork) -> Job:
        """Create a job and schedule its work. Returns immediately."""
        now = self._clock()
        job = Job(id=self._new_id(), kind=kind, created_at=now, updated_at=now)
        self._jobs[job.id] = job
        self._gc()
        task = asyncio.create_task(self._run(job, work))
        self._tasks[job.id] = task
        return job

    async def _run(self, job: Job, work: JobWork) -> None:
        job.status = RUNNING
        job.updated_at = self._clock()
        try:
            job.result = await work()
            job.status = SUCCEEDED
        except Exception as exc:  # noqa: BLE001 - generic client error (F-9)
            logger.error("job %s (%s) failed: %r", job.id, job.kind, exc)
            job.status = FAILED
            job.error = "transfer failed"
        finally:
            job.updated_at = self._clock()

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def status(self, job_id: str) -> dict[str, Any]:
        job = self._jobs.get(job_id)
        if job is None:
            return {"job_id": job_id, "status": "unknown"}
        return job.to_dict()

    def list(self) -> list[dict[str, Any]]:
        return [j.to_dict() for j in self._jobs.values()]

    async def wait(self, job_id: str) -> Optional[Job]:
        """Await a job's completion (test helper; not used on the hot path)."""
        task = self._tasks.get(job_id)
        if task is not None:
            await task
        return self._jobs.get(job_id)

    async def drain(self) -> None:
        """Await all in-flight jobs (used on shutdown / in tests)."""
        tasks = [t for t in self._tasks.values() if not t.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
