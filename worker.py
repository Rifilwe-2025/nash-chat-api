"""Celery worker entrypoint.

Run a worker::

    celery -A worker.celery_app worker --loglevel=info

Run the scheduler that enqueues due syncs::

    celery -A worker.celery_app beat --loglevel=info

Importing the task modules is what registers them: Celery discovers tasks by import, and a worker
that has not imported a module will reject its tasks as unregistered. This module exists to make
that import list explicit rather than relying on autodiscovery finding the right packages.
"""

from __future__ import annotations

from src.core.queue import celery_app

# Registers the knowledge base tasks on the app above. Imported for the side effect, which is the
# whole point of the module.
from src.modules.knowledge_base.internal import tasks as knowledge_base_tasks  # noqa: F401

__all__ = ["celery_app"]
