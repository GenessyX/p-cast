"""Fire-and-forget tasks that cannot be garbage collected while running.

The event loop only keeps weak references to tasks. A task whose result nobody
holds (e.g. the future returned by ``asyncio.run_coroutine_threadsafe`` and then
dropped) can be collected mid-execution together with everything only it
references. For a task that is connecting a ``pulsectl_asyncio.PulseAsync``, that
includes the ctypes callbacks registered with libpulse: libpulse later calls the
freed callback and the process segfaults.
"""

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task[Any]] = set()


def _on_done(task: asyncio.Task[Any]) -> None:
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Background task %s failed", task.get_name(), exc_info=exc)


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    """Start ``coro`` as a task on the running loop and keep it referenced until it finishes."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_on_done)
    return task


def spawn_threadsafe(loop: asyncio.AbstractEventLoop, coro: Coroutine[Any, Any, Any]) -> None:
    """Like ``spawn``, callable from any thread (replaces ``run_coroutine_threadsafe``)."""
    loop.call_soon_threadsafe(spawn, coro)
