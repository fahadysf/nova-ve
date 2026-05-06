"""Structured logging for the EVE-NG importer (#183).

stderr gets one JSON object per log event; stdout gets the human-readable run
summary at the end. ``--verbose`` flips DEBUG on; otherwise INFO is the floor.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Iterable

LOGGER_NAME = "nova_ve.import_eveng"


class _JsonFormatter(logging.Formatter):
    """One JSON object per log record, written to stderr."""

    _RESERVED = frozenset(
        {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "message",
            "asctime",
            "taskName",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in self._RESERVED or key.startswith("_"):
                continue
            try:
                json.dumps(value)
            except TypeError:
                value = repr(value)
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(verbose: bool = False) -> logging.Logger:
    """Install the JSON stderr handler on the importer's logger and return it.

    Idempotent: repeat calls clear any previously-installed handlers so test
    fixtures and re-runs do not produce duplicated output.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    logger.propagate = False

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(_JsonFormatter())
    logger.addHandler(handler)

    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    return logger


def write_summary(stream, manifest_dict: dict, *, manifest_path: str) -> None:
    """Write the human-readable run summary to ``stream`` (stdout by default)."""
    imported = len(manifest_dict.get("imported", []))
    templates = len(manifest_dict.get("templates", []))
    skipped = len(manifest_dict.get("skipped", []))
    errors = len(manifest_dict.get("errors", []))
    needs_review = sum(
        1 for t in manifest_dict.get("templates", []) if t.get("status") == "needs-manual-review"
    )

    lines: Iterable[str] = (
        "",
        "=== nova-ve EVE-NG importer — run summary ===",
        f"  imported files     : {imported}",
        f"  templates emitted  : {templates - needs_review}",
        f"  needs-manual-review: {needs_review}",
        f"  skipped (idempotent): {skipped}",
        f"  errors             : {errors}",
        f"  manifest           : {manifest_path}",
        "",
    )
    stream.write("\n".join(lines))
