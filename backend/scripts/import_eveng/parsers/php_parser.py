"""Parser for legacy EVE-NG PHP templates (#186).

Source: ``/opt/unetlab/html/templates/<vendor>.php``.

The legacy templates are PHP files declaring a small set of known variables.
We regex-extract the GH-spec'd subset (``$name``, ``$type``, ``$cpu``, ``$ram``,
``$ethernet``, ``$qemu_arch``, ``$qemu_nic``, ``$qemu_options``, ``$icon``,
``$console``) and drop the full file content under ``_eveng_raw.php_residual``
so the importer never silently loses data.

Per-field fail-graceful contract:

- File unreadable → :class:`ParserError` (importer marks file as ``errors[]``).
- File readable but unparseable as plain text → still readable as bytes; we
  capture residual unconditionally and only fail if even the binary read fails.
- Single field missing or malformed → omit from result; do not raise.
- Numeric field (``cpu``/``ram``/``ethernet``) malformed → omit from result.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


class ParserError(RuntimeError):
    """Raised on unrecoverable parser errors (e.g. file unreadable)."""


_STRING_FIELDS = ("name", "type", "qemu_arch", "qemu_nic", "qemu_options", "icon", "console")
_INT_FIELDS = ("cpu", "ram", "ethernet")


def _string_pattern(name: str) -> re.Pattern[str]:
    # Match $name = "value" or $name = 'value' (non-greedy capture).
    return re.compile(r"\$" + re.escape(name) + r"\s*=\s*['\"]([^'\"]*)['\"]")


def _int_pattern(name: str) -> re.Pattern[str]:
    # Match $name = 123 (no quotes).
    return re.compile(r"\$" + re.escape(name) + r"\s*=\s*(-?\d+)\b")


_STRING_PATTERNS = {field: _string_pattern(field) for field in _STRING_FIELDS}
_INT_PATTERNS = {field: _int_pattern(field) for field in _INT_FIELDS}


def parse_php(path: Path) -> dict[str, Any]:
    """Parse a PHP template file into a normalised intermediate dict.

    Each known field is extracted independently — a missing or malformed field
    is silently omitted from the result, never raised. Only file-unreadable
    aborts.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ParserError(f"PHP file unreadable: {path}: {exc}") from exc

    result: dict[str, Any] = {"_eveng_raw": {"php_residual": text}}

    for field, pattern in _STRING_PATTERNS.items():
        match = pattern.search(text)
        if match is not None:
            result[field] = match.group(1)

    for field, pattern in _INT_PATTERNS.items():
        match = pattern.search(text)
        if match is not None:
            try:
                result[field] = int(match.group(1))
            except ValueError:
                # Malformed number — silently omit per the contract.
                pass

    return result
