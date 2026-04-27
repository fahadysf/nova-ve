import fcntl
import time
from contextlib import contextmanager
from pathlib import Path


class LabLockTimeout(Exception):
    """Raised when the lab lock cannot be acquired within the timeout."""


@contextmanager
def lab_lock(lab_id: str, labs_dir: Path, timeout_s: float = 5.0):
    """Acquire an exclusive flock on LABS_DIR/{lab_id}.lock.

    - Opens the lock file fresh per request via 'a+' (no truncation).
    - Releases via fcntl.LOCK_UN in a finally block.
    - Times out by polling LOCK_NB in 50ms increments.
    - Lock fd MUST NOT be cached at module scope.
    """
    labs_dir = Path(labs_dir)
    labs_dir.mkdir(parents=True, exist_ok=True)
    lock_path = labs_dir / f"{lab_id}.lock"
    deadline = time.monotonic() + timeout_s
    with open(lock_path, 'a+') as fd:  # 'a+' avoids truncation
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LabLockTimeout(
                        f"Could not acquire lock on lab {lab_id} within {timeout_s}s"
                    )
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
