"""Parser for EVE-NG ``qemu_wrappers/<vendor>_wrapper.py`` shell scripts (#186).

EVE-NG wrappers are short Python (or shell) scripts that build the qemu argv.
We do not execute them — we scan for ``qemu-system-...`` invocations and
extract the trailing argv tokens so the adapter can fold them into
``extras.qemu_options``.

Returns a dict with ``{"qemu_flags": [<token>, ...]}``. Empty list if no
``qemu-system-...`` invocation is found.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

from .php_parser import ParserError

_QEMU_INVOCATION = re.compile(r"\bqemu-system-[a-z0-9_]+\b")


def parse_wrapper(path: Path) -> dict[str, Any]:
    """Read a wrapper script and extract qemu argv flags."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ParserError(f"wrapper unreadable: {path}: {exc}") from exc

    flags: list[str] = []
    for line in text.splitlines():
        match = _QEMU_INVOCATION.search(line)
        if not match:
            continue
        # Tokenise the line as if it were a shell command and take everything
        # after the qemu-system-* token. shlex handles quoted args correctly.
        try:
            tokens = shlex.split(line, comments=True)
        except ValueError:
            # Unbalanced quotes in a non-shell context — skip this line.
            continue
        for idx, tok in enumerate(tokens):
            if _QEMU_INVOCATION.match(tok):
                flags.extend(tokens[idx + 1 :])
                break

    return {"qemu_flags": flags, "_eveng_raw": {"wrapper": {"text": text, "qemu_flags": flags}}}
