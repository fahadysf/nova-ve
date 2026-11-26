import asyncio
import fcntl
import time
from contextlib import asynccontextmanager, contextmanager, nullcontext
from pathlib import Path


class LabLockTimeout(Exception):
    """Raised when a lab or folder lock cannot be acquired within the timeout."""


def _lock_path(lab_id: str, labs_dir: Path, create_parent: bool) -> Path:
    labs_dir = Path(labs_dir)
    labs_dir.mkdir(parents=True, exist_ok=True)
    path = labs_dir / f"{lab_id}.lock"
    if create_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _namespace_path(labs_dir: Path) -> Path:
    labs_dir = Path(labs_dir)
    labs_dir.mkdir(parents=True, exist_ok=True)
    return labs_dir / ".folder-namespace.lock"


def _try_lock(fd, mode: int) -> bool:
    try:
        fcntl.flock(fd, mode | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _timeout(lab_id: str, timeout_s: float) -> LabLockTimeout:
    return LabLockTimeout(f"Could not acquire lock on lab {lab_id} within {timeout_s}s")


@contextmanager
def _file_lock(path: Path, mode: int, lab_id: str, timeout_s: float, deadline: float):
    with open(path, 'a+') as fd:
        while not _try_lock(fd, mode):
            if time.monotonic() >= deadline:
                raise _timeout(lab_id, timeout_s)
            time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


@asynccontextmanager
async def _file_lock_async(path: Path, mode: int, lab_id: str, timeout_s: float, deadline: float):
    with open(path, 'a+') as fd:
        while not _try_lock(fd, mode):
            if time.monotonic() >= deadline:
                raise _timeout(lab_id, timeout_s)
            await asyncio.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def lab_lock(
    lab_id: str,
    labs_dir: Path,
    timeout_s: float = 5.0,
    *,
    namespace: bool = True,
    create_parent: bool = True,
):
    """Share the folder namespace, then exclusively lock the lab's adjacent file."""
    deadline = time.monotonic() + timeout_s
    shared = (
        _file_lock(_namespace_path(labs_dir), fcntl.LOCK_SH, lab_id, timeout_s, deadline)
        if namespace else nullcontext()
    )
    with shared:
        path = _lock_path(lab_id, labs_dir, create_parent)
        with _file_lock(path, fcntl.LOCK_EX, lab_id, timeout_s, deadline):
            yield


@asynccontextmanager
async def lab_lock_async(
    lab_id: str,
    labs_dir: Path,
    timeout_s: float = 5.0,
    *,
    namespace: bool = True,
    create_parent: bool = True,
):
    """Acquire the same locks as :func:`lab_lock` without blocking while waiting."""
    deadline = time.monotonic() + timeout_s
    shared = (
        _file_lock_async(_namespace_path(labs_dir), fcntl.LOCK_SH, lab_id, timeout_s, deadline)
        if namespace else nullcontext()
    )
    async with shared:
        path = _lock_path(lab_id, labs_dir, create_parent)
        async with _file_lock_async(path, fcntl.LOCK_EX, lab_id, timeout_s, deadline):
            yield


@asynccontextmanager
async def folder_namespace_lock_async(labs_dir: Path, timeout_s: float = 5.0, *, shared: bool = False):
    """Guard folder membership with a lock outside movable lab folders."""
    deadline = time.monotonic() + timeout_s
    mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    async with _file_lock_async(_namespace_path(labs_dir), mode, "folder namespace", timeout_s, deadline):
        yield
