"""Helpers for thread-backed mutations and request cancellation."""

import asyncio
import contextvars
import functools
import logging


logger = logging.getLogger(__name__)
CANCELLED_NOTIFICATION_TIMEOUT_S = 5.0


async def run_mutation(function, *args, **kwargs):
    """Wait for an already-started worker, recording cancellation requests.

    A worker cannot be cancelled safely: it may own a caller's lock or have
    already changed persistent state. Keep the caller alive until it finishes.
    A worker exception takes precedence over a remembered cancellation.
    """
    context = contextvars.copy_context()
    call = functools.partial(function, *args, **kwargs)
    future = asyncio.get_running_loop().run_in_executor(None, context.run, call)
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
    return future.result(), cancelled


async def notify_after_mutation(coroutine, cancelled):
    """Attempt a notification once; limit cleanup time after cancellation."""
    if not cancelled:
        return await coroutine
    try:
        await asyncio.wait_for(coroutine, timeout=CANCELLED_NOTIFICATION_TIMEOUT_S)
    except asyncio.CancelledError:
        logger.warning("Notification interrupted after request cancellation")
    except Exception:
        logger.exception("Notification failed after request cancellation")
    raise asyncio.CancelledError
