import asyncio
from collections.abc import Awaitable, Callable, Hashable
import logging
import time
from typing import Any


_logger = logging.getLogger("nova-ve.node_catalog")

_Catalog = dict[str, Any]
_CatalogResult = tuple[_Catalog, bool]


class NodeCatalogService:
    """Share catalog builds and retain the last successful snapshot.

    Freshness is access-driven: a GET at least ``max_age_seconds`` after the
    last successful build receives the stale snapshot immediately while one
    background rebuild runs. A cold build may supply a partial snapshot marked
    pending while an optional discovery dependency is unavailable.
    """

    def __init__(
        self,
        build: Callable[[], _Catalog],
        *,
        build_degraded: Callable[[Exception], _Catalog | None] | None = None,
        on_updated: Callable[[int], Awaitable[None]] | None = None,
        max_age_seconds: float = 30,
        retry_delay_seconds: float = 5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._build_catalog = build
        self._build_degraded_catalog = build_degraded
        self._on_updated = on_updated
        self._max_age_seconds = max_age_seconds
        self._retry_delay_seconds = retry_delay_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._snapshot: _Catalog | None = None
        self._snapshot_key: Hashable | None = None
        self._built_at: float | None = None
        self._generation = 0
        self._task: asyncio.Task[_CatalogResult] | None = None
        self._worker: asyncio.Task[_CatalogResult] | None = None
        self._building_key: Hashable | None = None
        self._retry_after = 0.0
        self._notification_task: asyncio.Task[None] | None = None
        self._pending_generation: int | None = None
        self._closed = False

    def start(self) -> None:
        self._closed = False

    async def get(self, cache_key: Hashable | None = None) -> _Catalog:
        catalog, _pending = await self.get_with_pending(cache_key)
        return catalog

    async def get_with_pending(
        self, cache_key: Hashable | None = None
    ) -> _CatalogResult:
        while True:
            other_build: asyncio.Task[_CatalogResult] | None = None
            cold_build: asyncio.Task[_CatalogResult] | None = None
            async with self._lock:
                if self._closed:
                    raise RuntimeError("Node catalog service is shut down.")

                if self._task is not None and self._building_key != cache_key:
                    other_build = self._task
                elif self._task is None and self._worker is not None and not self._worker.done():
                    # A cancelled catalog request can leave its thread finishing a
                    # synchronous read. Keep a restart from launching another one.
                    other_build = self._worker
                else:
                    if self._snapshot_key != cache_key:
                        self._snapshot = None
                        self._snapshot_key = cache_key

                    now = self._clock()
                    if (
                        self._snapshot is not None
                        and self._built_at is not None
                        and now - self._built_at < self._max_age_seconds
                    ):
                        return self._snapshot, False
                    if now < self._retry_after:
                        if self._snapshot is not None:
                            return self._snapshot, True
                        raise RuntimeError("Node catalog refresh is backing off after a failure.")

                    task = self._start_build_locked(cache_key)
                    if self._snapshot is not None:
                        return self._snapshot, True
                    cold_build = task

            if other_build is not None:
                try:
                    await asyncio.shield(other_build)
                except Exception:
                    pass
                continue

            if cold_build is not None:
                return await asyncio.shield(cold_build)

    def _start_build_locked(self, cache_key: Hashable | None) -> asyncio.Task[_CatalogResult]:
        if self._task is None:
            self._building_key = cache_key
            cold = self._snapshot is None
            self._task = asyncio.create_task(self._build(cache_key, cold=cold))
            self._task.add_done_callback(self._consume_build_exception)
        return self._task

    def _build_snapshot(self, cold: bool) -> _CatalogResult:
        try:
            return self._build_catalog(), False
        except Exception as error:
            if cold and self._build_degraded_catalog is not None:
                degraded = self._build_degraded_catalog(error)
                if degraded is not None:
                    _logger.warning("Serving a partial node catalog after discovery failed: %s", error)
                    return degraded, True
            raise

    async def _build(self, cache_key: Hashable | None, *, cold: bool) -> _CatalogResult:
        task = asyncio.current_task()
        worker = asyncio.create_task(asyncio.to_thread(self._build_snapshot, cold))
        self._worker = worker
        worker.add_done_callback(self._finish_worker)
        try:
            catalog, pending = await asyncio.shield(worker)
        except asyncio.CancelledError:
            async with self._lock:
                if self._task is task:
                    self._task = None
                    self._building_key = None
            raise
        except Exception:
            async with self._lock:
                self._retry_after = self._clock() + self._retry_delay_seconds
                if self._task is task:
                    self._task = None
                    self._building_key = None
            _logger.exception("Node catalog background build failed.")
            raise

        async with self._lock:
            if self._closed:
                raise asyncio.CancelledError
            changed = self._built_at is None or self._snapshot != catalog
            self._snapshot = catalog
            self._snapshot_key = cache_key
            now = self._clock()
            self._built_at = None if pending else now
            self._retry_after = now + self._retry_delay_seconds if pending else 0.0
            if self._task is task:
                self._task = None
                self._building_key = None
            if not pending and changed:
                self._generation += 1
                self._schedule_notification_locked(self._generation)
        return catalog, pending

    def _schedule_notification_locked(self, generation: int) -> None:
        if self._on_updated is None:
            return
        self._pending_generation = generation
        if self._notification_task is None:
            self._notification_task = asyncio.create_task(self._notify())
            self._notification_task.add_done_callback(self._consume_notification_exception)

    async def _notify(self) -> None:
        while True:
            async with self._lock:
                generation = self._pending_generation
                self._pending_generation = None
                if generation is None or self._closed:
                    self._notification_task = None
                    return
            try:
                await self._on_updated(generation)
            except Exception:
                _logger.exception("Node catalog update notification failed.")

    @staticmethod
    def _consume_notification_exception(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    @staticmethod
    def _consume_build_exception(task: asyncio.Task[_CatalogResult]) -> None:
        if not task.cancelled():
            task.exception()

    def _finish_worker(self, worker: asyncio.Task[_CatalogResult]) -> None:
        if self._worker is worker:
            self._worker = None
        self._consume_build_exception(worker)

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            task = self._task
            notification_task = self._notification_task
            if task is not None:
                task.cancel()
            if notification_task is not None:
                notification_task.cancel()
            self._task = None
            self._building_key = None
            self._notification_task = None
            self._pending_generation = None
        for pending in (task, notification_task):
            if pending is not None:
                try:
                    await pending
                except (asyncio.CancelledError, Exception):
                    pass
