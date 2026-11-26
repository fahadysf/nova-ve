"""Helpers for mutations and request cancellation."""

import asyncio
import contextvars
import functools
import logging


logger = logging.getLogger(__name__)
CANCELLED_NOTIFICATION_TIMEOUT_S = 5.0


async def run_mutation(function, *args, **kwargs):
    """Run a synchronous mutation in a worker and wait for it to finish."""
    context = contextvars.copy_context()
    call = functools.partial(function, *args, **kwargs)
    future = asyncio.get_running_loop().run_in_executor(None, context.run, call)
    return await wait_for_mutation(future)


async def wait_for_mutation(future):
    """Let an in-flight mutation finish and remember caller cancellation.

    The mutation may hold a lock or have changed persistent state. An error
    from the mutation takes precedence over a remembered cancellation.
    """
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
