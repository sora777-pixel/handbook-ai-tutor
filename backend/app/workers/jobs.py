from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.services.pipeline import IngestPipeline

logger = logging.getLogger("app.worker")

#: How many ingests may parse at the same time in this process.
#:
#: Uploads are meant to be concurrent -- three files should be parsed side by
#: side -- but they all share one event loop, one SQLite file and one set of API
#: keys. Letting twelve of them run at once turns into a queue of timeouts and
#: rate-limit errors, which looks like "the app is broken" rather than "busy".
#: So: real parallelism up to the cap, a short FIFO queue beyond it, and every
#: job keeps its own database session either way. ARQ has its own max_jobs for
#: the multi-process case; this covers the single-process inline queue that
#: start.bat uses.
_CONCURRENCY: asyncio.Semaphore | None = None
_CONCURRENCY_LIMIT: int | None = None


def _semaphore() -> asyncio.Semaphore:
    """Lazily built so the limit can be configured (and tests can shrink it)."""
    global _CONCURRENCY, _CONCURRENCY_LIMIT
    limit = max(1, int(get_settings().ingest_concurrency or 3))
    if _CONCURRENCY is None or _CONCURRENCY_LIMIT != limit:
        _CONCURRENCY = asyncio.Semaphore(limit)
        _CONCURRENCY_LIMIT = limit
    return _CONCURRENCY


def set_ingest_concurrency(limit: int) -> None:
    """Test hook: rebuild the gate with a different width."""
    global _CONCURRENCY, _CONCURRENCY_LIMIT
    _CONCURRENCY_LIMIT = None
    _CONCURRENCY = asyncio.Semaphore(max(1, int(limit)))


async def ingest_source(ctx: dict, source_id: str, task_id: str) -> None:
    """ARQ job (and inline queue) entrypoint.

    Each job owns its session, its progress row and its source row, so one
    failing upload can only mark *its own* source as failed. The semaphore
    bounds how many run at once; it never merges or cancels work.
    """
    async with _semaphore():
        async with SessionLocal() as session:
            pipeline = IngestPipeline(session)
            await pipeline.run(UUID(source_id), UUID(task_id))


class WorkerSettings:
    functions = [ingest_source]
    redis_settings = None
    max_jobs = 4
    job_timeout = 60 * 30

    def __init__(self) -> None:
        from arq.connections import RedisSettings

        type(self).redis_settings = RedisSettings.from_dsn(get_settings().redis_url)