import asyncio

import pytest

from ipixel_mcp.jobs import JobRegistry, SUCCEEDED, FAILED, QUEUED


def test_submit_returns_immediately_and_succeeds():
    async def scenario():
        reg = JobRegistry()
        ran = {"v": False}

        async def work():
            ran["v"] = True
            return {"done": 1}

        job = reg.submit("display_image", work)
        assert job.status in (QUEUED, "running")  # not yet finished
        done = await reg.wait(job.id)
        assert done.status == SUCCEEDED
        assert done.result == {"done": 1}
        assert ran["v"] is True

    asyncio.run(scenario())


def test_failure_is_generic():
    async def scenario():
        reg = JobRegistry()

        async def work():
            raise RuntimeError("internal /secret/path detail")

        job = reg.submit("x", work)
        done = await reg.wait(job.id)
        assert done.status == FAILED
        assert done.error == "transfer failed"  # no internal leak
        assert "secret" not in (done.error or "")

    asyncio.run(scenario())


def test_status_unknown():
    reg = JobRegistry()
    assert reg.status("nope")["status"] == "unknown"


def test_status_dict_shape():
    async def scenario():
        reg = JobRegistry()
        job = reg.submit("k", lambda: _ok())
        await reg.wait(job.id)
        s = reg.status(job.id)
        assert s["job_id"] == job.id
        assert s["kind"] == "k"
        assert set(["job_id", "status", "result", "error", "created_at", "updated_at"]).issubset(s)

    async def _ok():
        return 1

    asyncio.run(scenario())


def test_gc_drops_old_terminal_jobs():
    async def scenario():
        reg = JobRegistry(max_jobs=3)

        async def w():
            return 1

        ids = []
        for _ in range(6):
            ids.append(reg.submit("k", w).id)
            await reg.drain()
        assert len(reg.list()) <= 3

    asyncio.run(scenario())
