"""Logging + progress output for the EVE-NG importer (#183, #271).

Three output surfaces:

- **Terminal stdout**: numbered, human-readable step lines via
  :class:`StepReporter` (``[3/12] qemu/vyos-1.4 — moving hda.qcow2 (2.1 GB)
  ... done (0.3s, symlinked back to ...)``) plus the final run summary.
  Suppressed under ``--json``.
- **Terminal stderr**: one JSON object per log event, only under ``--json``
  (the pre-#271 machine-readable stream).
- **Log file**: appended at ``/var/log/eve-ng-importer.log`` (override with
  ``--log-file``) with size-capped rotation. Step lines and summaries are
  mirrored here with timestamps so post-run debugging never depends on
  terminal scrollback.

``--verbose`` flips the level to DEBUG; otherwise INFO is the floor.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import IO, Iterator

LOGGER_NAME = "nova_ve.import_eveng"

DEFAULT_LOG_PATH = Path("/var/log/eve-ng-importer.log")
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

_logger = logging.getLogger(LOGGER_NAME)

_RESERVED_ATTRS = frozenset(
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


class _JsonFormatter(logging.Formatter):
    """One JSON object per log record, written to stderr under ``--json``."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS or key.startswith("_"):
                continue
            try:
                json.dumps(value)
            except TypeError:
                value = repr(value)
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class _PlainFormatter(logging.Formatter):
    """Timestamped single-line records for the log file: ``ts LEVEL msg k=v``."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
            timespec="seconds"
        )
        line = f"{ts} {record.levelname:<5} {record.getMessage()}"
        extras = " ".join(
            f"{key}={value}"
            for key, value in sorted(record.__dict__.items())
            if key not in _RESERVED_ATTRS and not key.startswith("_")
        )
        if extras:
            line += f" {extras}"
        return line


def configure_logging(
    verbose: bool = False,
    *,
    json_stream: bool = False,
    log_path: Path | None = None,
) -> logging.Logger:
    """Install handlers on the importer's logger and return it.

    - A rotating file handler is always attempted at ``log_path`` (default
      ``/var/log/eve-ng-importer.log``). When the path is not writable (e.g.
      a non-root ``--dry-run``) it is skipped with a one-line stderr note and
      the run continues without a log file.
    - The JSON stderr stream is only installed under ``json_stream``
      (``--json``); the default human mode keeps stderr quiet.
    - ``verbose`` flips the level to DEBUG; otherwise INFO is the floor.

    Idempotent: repeat calls clear any previously-installed handlers so test
    fixtures and re-runs do not produce duplicated output.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    path = log_path if log_path is not None else DEFAULT_LOG_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(_PlainFormatter())
        logger.addHandler(file_handler)
    except OSError as exc:
        print(
            f"import_eveng: cannot write log file {path}: {exc}; continuing without it.",
            file=sys.stderr,
        )

    if json_stream:
        handler = logging.StreamHandler(stream=sys.stderr)
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)

    return logger


# ---- human-readable step output (#271) -----------------------------------


def human_size(num_bytes: int) -> str:
    """Render a byte count compactly, e.g. ``42 B`` / ``2.1 GiB``."""
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024.0 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TiB"  # pragma: no cover — loop returns by the TiB step


class _StepLine:
    """One in-flight step line; completes itself with done/skipped/FAILED."""

    def __init__(self, reporter: "StepReporter", prefix: str, started: float) -> None:
        self._reporter = reporter
        self._prefix = prefix
        self._started = started

    def finish(self, detail: str | None = None) -> None:
        """Complete the line with the elapsed time (plus optional detail)."""
        elapsed = time.monotonic() - self._started
        suffix = f"done ({elapsed:.1f}s, {detail})" if detail else f"done ({elapsed:.1f}s)"
        self._emit(suffix)

    def skip(self, reason: str | None = None) -> None:
        self._emit(f"skipped ({reason})" if reason else "skipped")

    def fail(self, error: str | None = None) -> None:
        self._emit(f"FAILED ({error})" if error else "FAILED")

    def _emit(self, suffix: str) -> None:
        self._reporter._emit_line(self._prefix, suffix)


class StepReporter:
    """Renders numbered, human-readable progress lines to a stream.

    Each :meth:`step` context writes ``[i/n] label ... `` on entry and
    completes the line with ``done (<elapsed>s, <detail>)``, ``skipped
    (<reason>)`` or ``FAILED (<error>)`` on exit. Every completed line is
    mirrored into the importer's log logger so it lands in the log file.
    """

    def __init__(self, stream: IO[str], total: int, *, enabled: bool = True) -> None:
        self._stream = stream
        self._total = max(total, 1)
        self._enabled = enabled
        self._index = 0

    @contextmanager
    def step(self, label: str) -> Iterator[_StepLine]:
        self._index += 1
        prefix = f"[{self._index}/{self._total}] {label}"
        if self._enabled:
            self._stream.write(f"{prefix} ... ")
            self._stream.flush()
        line = _StepLine(self, prefix, time.monotonic())
        try:
            yield line
        except Exception as exc:
            line.fail(str(exc) or type(exc).__name__)
            raise

    def _emit_line(self, prefix: str, suffix: str) -> None:
        if self._enabled:
            self._stream.write(f"{suffix}\n")
            self._stream.flush()
        _logger.info("importer.step", extra={"step": prefix, "result": suffix})


class _NullLine:
    """No-op stand-in for :class:`_StepLine`."""

    def finish(self, detail: str | None = None) -> None:
        pass

    def skip(self, reason: str | None = None) -> None:
        pass

    def fail(self, error: str | None = None) -> None:
        pass


class NullStepReporter:
    """Silent reporter for programmatic callers that want no step output."""

    @contextmanager
    def step(self, label: str) -> Iterator[_NullLine]:
        yield _NullLine()


NULL_REPORTER = NullStepReporter()


def write_summary(
    stream,
    manifest_dict: dict,
    *,
    manifest_path: str,
    log_path: str | None = None,
) -> None:
    """Write the human-readable run summary to ``stream`` (stdout by default)."""
    imported = len(manifest_dict.get("imported", []))
    templates = len(manifest_dict.get("templates", []))
    skipped = len(manifest_dict.get("skipped", []))
    errors = len(manifest_dict.get("errors", []))
    needs_review = sum(
        1 for t in manifest_dict.get("templates", []) if t.get("status") == "needs-manual-review"
    )

    lines: tuple[str, ...] = (
        "",
        "=== nova-ve EVE-NG importer — run summary ===",
        f"  imported files     : {imported}",
        f"  templates emitted  : {templates - needs_review}",
        f"  needs-manual-review: {needs_review}",
        f"  skipped (idempotent): {skipped}",
        f"  errors             : {errors}",
        f"  manifest           : {manifest_path}",
    )
    extra: tuple[str, ...] = ()
    if log_path:
        extra = (f"  log                : {log_path}",)
        if errors:
            extra += (f"  note               : see {log_path} for error details",)
    lines += extra + ("",)
    stream.write("\n".join(lines))
