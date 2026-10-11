from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.task import Task

logger = logging.getLogger("app.progress")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProgressService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.settings = get_settings()

    async def update(
        self,
        task_id: UUID,
        *,
        progress: int,
        step: str,
        status: str | None = None,
        message: str | None = None,
    ) -> Task | None:
        # The task row can be gone: a source deleted mid-parse takes its task
        # with it (cascade). An update then has nowhere to land, which is not
        # an error worth a traceback.
        task = (await self.session.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
        if task is None:
            logger.debug("progress update for a deleted task %s ignored", task_id)
            return None
        task.progress = max(0, min(100, progress))
        task.step = step
        if status:
            task.status = status
        if message is not None:
            task.message = message
        task.updated_at = _utcnow()
        await self.session.commit()
        await self.session.refresh(task)
        await self._publish(task)
        return task

    async def _publish(self, task: Task) -> None:
        payload = json.dumps(
            {
                "id": str(task.id),
                "source_id": str(task.source_id),
                "status": task.status,
                "progress": task.progress,
                "step": task.step,
                "message": task.message,
            }
        )
        try:
            import redis.asyncio as redis

            # Short timeouts: without Redis (docker-less dev) the default connect
            # timeout makes every progress update block for seconds on Windows.
            client = redis.from_url(
                self.settings.redis_url,
                socket_connect_timeout=0.3,
                socket_timeout=0.5,
            )
            await client.set(f"task:{task.id}:progress", payload, ex=3600)
            await client.publish(f"task:{task.id}", payload)
            await client.aclose()
        except Exception:
            # SSE can still poll the database when Redis is down (inline/dev).
            return
