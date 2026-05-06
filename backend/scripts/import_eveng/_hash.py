"""sha256 streaming helper for the EVE-NG importer (#183).

Used by the CLI's idempotency check and by #184's copy-then-verify-then-delete
semantics. A streaming helper keeps memory bounded for multi-GB image files.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import BinaryIO

_DEFAULT_CHUNK_SIZE = 1024 * 1024  # 1 MiB


def sha256_stream(stream: BinaryIO, chunk_size: int = _DEFAULT_CHUNK_SIZE) -> str:
    """Compute the sha256 hex digest of ``stream`` reading in fixed-size chunks."""
    h = hashlib.sha256()
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        h.update(chunk)
    return h.hexdigest()


def sha256_file(path: Path, chunk_size: int = _DEFAULT_CHUNK_SIZE) -> str:
    """Compute the sha256 hex digest of the file at ``path``."""
    with path.open("rb") as fh:
        return sha256_stream(fh, chunk_size=chunk_size)
