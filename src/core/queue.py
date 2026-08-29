"""The background queue — wiring only, no business logic (spec §4).

Tasks themselves live in the owning module's ``internal/tasks.py``; this file knows how to run one,
never what one does. That separation is why ``core`` can stay free of feature knowledge while every
module gets background work.

**Celery rather than RQ** for one reason that matters here: the beat scheduler. Phase 9 needs
periodic re-sync, and Celery ships a scheduler that does not fork per job — which also happens to be
what makes it usable on Windows, where the maintainer develops.

**The async bridge.** Celery workers are synchronous and the application is async, so each task
opens its own event loop, engine and session. A worker process is not a web request: it must not
borrow the API's engine, and a task that fails rolls back its own transaction and nothing else.

**Modes.** ``redis`` is the real one — work leaves the request and a worker picks it up. ``inline``
runs the work in the caller instead, which is what makes local development and the test suite work
without a broker. Inline is a convenience and is honest about it: the request still blocks, so it is
not the mode any deployment should use.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from celery import Celery
from celery.schedules import crontab
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src import configs
from src.shared.database.engine import create_engine

logger = logging.getLogger("api.queue")

T = TypeVar("T")

INLINE = "inline"
REDIS = "redis"


def queue_mode() -> str:
    return (configs.QUEUE_MODE or INLINE).strip().lower()


def is_inline() -> bool:
    """True when work should run in the caller rather than being handed to a worker."""
    return queue_mode() != REDIS


celery_app = Celery(
    "nash",
    broker=configs.REDIS_URL,
    backend=configs.REDIS_URL,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # A task that dies with its worker must come back rather than vanish. The cost is that a task
    # can run twice, which is why the ingestion tasks are written to be safe to repeat.
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    # A stuck extraction must not hold a worker for ever. The soft limit raises inside the task so
    # it can record its own failure; the hard limit kills it if that does not work.
    task_soft_time_limit=configs.QUEUE_TASK_SOFT_TIME_LIMIT_SECONDS,
    task_time_limit=configs.QUEUE_TASK_TIME_LIMIT_SECONDS,
    beat_schedule={
        "sweep-due-sources": {
            "task": "knowledge_base.sweep_due_sources",
            # Every minute, rather than a schedule per source: intervals are configured per source
            # in the database, and a static beat schedule cannot follow rows that change. The sweep
            # asks "what is due?" and enqueues it, so adding a source needs no scheduler restart.
            "schedule": crontab(minute="*"),
        },
    },
)


def run_async(coro_factory: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Run one async unit of work in a worker, with a session of its own.

    The engine is created and disposed per task rather than shared. Celery may fork or run tasks in
    threads, and an asyncpg pool created in one process and used from another is a source of
    intermittent, extremely confusing failures.
    """

    async def runner() -> T:
        engine = create_engine()
        factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        try:
            async with factory() as session:
                try:
                    result = await coro_factory(session)
                except Exception:
                    await session.rollback()
                    raise
                await session.commit()
                return result
        finally:
            await engine.dispose()

    return asyncio.run(runner())


def enqueue(task: Any, *args: Any, **kwargs: Any) -> None:
    """Hand a task to a worker.

    Failure to *enqueue* is logged and swallowed. The caller is a request that has already committed
    its own work — a broker blip should leave a source waiting for the next sweep, not fail an
    upload the tenant has already been told succeeded.
    """
    try:
        task.delay(*args, **kwargs)
    except Exception:
        logger.exception("could not enqueue %s; it will be retried by the next sweep", task.name)
