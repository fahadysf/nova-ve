import ast
import threading
import time
from pathlib import Path

import pytest

from app.services.lab_lock import LabLockTimeout, lab_lock


def test_concurrent_mutation_serialized(tmp_path):
    """Two threads acquiring the lock must not interleave their writes."""
    shared_file = tmp_path / "shared.txt"
    lab_id = "test-lab"

    def writer(tag: str):
        with lab_lock(lab_id, tmp_path):
            with open(shared_file, "a") as f:
                f.write(f"{tag}-start\n")
            time.sleep(0.1)
            with open(shared_file, "a") as f:
                f.write(f"{tag}-end\n")

    t1 = threading.Thread(target=writer, args=("A",))
    t2 = threading.Thread(target=writer, args=("B",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    lines = shared_file.read_text().splitlines()
    assert len(lines) == 4

    # Thread A's lines must be contiguous OR thread B's lines must be contiguous.
    # i.e. never alternating A-start, B-start, A-end, B-end
    if lines[0].startswith("A"):
        assert lines[0] == "A-start"
        assert lines[1] == "A-end"
        assert lines[2] == "B-start"
        assert lines[3] == "B-end"
    else:
        assert lines[0] == "B-start"
        assert lines[1] == "B-end"
        assert lines[2] == "A-start"
        assert lines[3] == "A-end"


def test_lock_fd_not_cached_at_module_scope():
    """No module-level open() call exists in lab_lock.py."""
    import app.services.lab_lock as mod
    source_path = Path(mod.__file__)
    source = source_path.read_text()
    tree = ast.parse(source)

    # Only inspect statements that are directly at module scope,
    # skipping into function/class bodies (which are allowed to call open).
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                func = child.func
                name = None
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                if name == "open":
                    pytest.fail(
                        f"Found module-level open() call at line {child.lineno} in {source_path}"
                    )


def test_lock_released_on_handler_exception(tmp_path):
    """Lock must be released even when the body raises an exception."""
    lab_id = "test-lab-exc"

    with pytest.raises(RuntimeError):
        with lab_lock(lab_id, tmp_path):
            raise RuntimeError("simulated failure")

    # Should be acquirable immediately after the exception
    acquired = False
    with lab_lock(lab_id, tmp_path, timeout_s=1.0):
        acquired = True
    assert acquired


def test_lock_timeout_raises_LabLockTimeout(tmp_path):
    """Thread B must raise LabLockTimeout when thread A holds the lock."""
    lab_id = "test-lab-timeout"
    lock_held = threading.Event()
    release_lock = threading.Event()
    error_in_a = []

    def holder():
        try:
            with lab_lock(lab_id, tmp_path, timeout_s=5.0):
                lock_held.set()
                release_lock.wait(timeout=5.0)
        except Exception as e:
            error_in_a.append(e)

    t_a = threading.Thread(target=holder)
    t_a.start()
    lock_held.wait(timeout=2.0)

    start = time.monotonic()
    with pytest.raises(LabLockTimeout):
        with lab_lock(lab_id, tmp_path, timeout_s=0.5):
            pass
    elapsed = time.monotonic() - start

    release_lock.set()
    t_a.join()

    assert not error_in_a, f"Thread A raised unexpectedly: {error_in_a}"
    # Should have timed out within ~0.5s (+/- 0.2s tolerance)
    assert elapsed < 1.0, f"Timeout took too long: {elapsed:.2f}s"


def test_lock_file_uses_a_plus_mode():
    """lab_lock.py must use 'a+' mode and must NOT use 'w' mode for the lock file."""
    import app.services.lab_lock as mod
    source = Path(mod.__file__).read_text()
    assert "'a+'" in source, "Expected 'a+' mode in lab_lock.py"
    # Ensure no truncating write mode for the lock file open
    assert "'w'" not in source, "Found 'w' mode in lab_lock.py — must not truncate"


def test_lock_timeout_returns_503():
    """An endpoint that raises LabLockTimeout responds with HTTP 503 + Retry-After: 2."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.main import lab_lock_timeout_handler

    app = FastAPI()
    app.add_exception_handler(LabLockTimeout, lab_lock_timeout_handler)

    @app.get("/__busy__")
    def busy():
        raise LabLockTimeout("Could not acquire lock on lab demo within 5.0s")

    with TestClient(app) as client:
        resp = client.get("/__busy__")

    assert resp.status_code == 503
    assert resp.headers.get("retry-after") == "2"
    body = resp.json()
    assert body["code"] == 503
    assert body["status"] == "fail"
    assert "lock" in body["message"].lower()
