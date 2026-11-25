import asyncio
import fcntl
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path


class LabLockTimeout(Exception):
    """Raised when the lab lock cannot be acquired within the timeout."""


def _lock_path(lab_id: str, labs_dir: Path) -> Path:
    labs_dir = Path(labs_dir)
    labs_dir.mkdir(parents=True, exist_ok=True)
    lock_path = labs_dir / f"{lab_id}.lock"
    # Issue #174 follow-up: when lab_id contains path separators (nested
    # labs like "nested/lab.json"), the lock file's parent directory may
    # not exist. Create it so open(...) doesn't fail with FileNotFoundError.
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    return lock_path


def _try_lock(fd) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _timeout(lab_id: str, timeout_s: float) -> LabLockTimeout:
    return LabLockTimeout(f"Could not acquire lock on lab {lab_id} within {timeout_s}s")


@contextmanager
def lab_lock(lab_id: str, labs_dir: Path, timeout_s: float = 5.0):
    """Acquire an exclusive flock on LABS_DIR/{lab_id}.lock.

    - Opens the lock file fresh per request via 'a+' (no truncation).
    - Releases via fcntl.LOCK_UN in a finally block.
    - Times out by polling LOCK_NB in 50ms increments.
    - Lock fd MUST NOT be cached at module scope.
    - Not re-entrant: each acquisition opens its own file description, so a
      holder that asks again (any thread, same process) waits for itself.
    """
    lock_path = _lock_path(lab_id, labs_dir)
    deadline = time.monotonic() + timeout_s
    with open(lock_path, 'a+') as fd:  # 'a+' avoids truncation
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise _timeout(lab_id, timeout_s)
            time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


@asynccontextmanager
async def lab_lock_async(lab_id: str, labs_dir: Path, timeout_s: float = 5.0):
    """:func:`lab_lock` for coroutines: waits without blocking the event loop.

    The same flock, so it excludes :func:`lab_lock` holders in worker threads.
    Do not await anything inside that needs this lab's lock.
    """
    lock_path = _lock_path(lab_id, labs_dir)
    deadline = time.monotonic() + timeout_s
    with open(lock_path, 'a+') as fd:  # 'a+' avoids truncation
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise _timeout(lab_id, timeout_s)
            await asyncio.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
