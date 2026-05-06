"""Parser for modern EVE-NG YAML templates (#186).

Source: ``/opt/unetlab/html/templates/intel/<vendor>.yml``.

Returns a normalised intermediate dict: every YAML key is preserved at the top
level (so adapter ``match()`` can inspect raw fields), and the original parsed
mapping is re-attached under ``_eveng_raw.yaml`` for adapters that need the
full content.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .php_parser import ParserError


def parse_yaml(path: Path) -> dict[str, Any]:
    """Parse a YAML template file. Raise :class:`ParserError` on malformed YAML."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ParserError(f"YAML parse failed for {path}: {exc}") from exc

    if data is None:
        return {"_eveng_raw": {"yaml": {}}}
    if not isinstance(data, dict):
        raise ParserError(f"YAML root must be a mapping in {path}, got {type(data).__name__}")

    result: dict[str, Any] = dict(data)
    result["_eveng_raw"] = {"yaml": dict(data)}
    return result
