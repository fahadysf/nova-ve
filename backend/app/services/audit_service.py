# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""User activity audit trail (#297).

Every security-relevant or destructive user action is recorded as one row
of the ``audit_log`` table: who acted, from where, on what, and in which
lab. Routers call :func:`record` once per *successful* action, after the
mutation has been applied, so every row describes something that actually
happened (failed sign-ins are the deliberate exception — they are the
brute-force signal).

Two properties shape the writer:

* **It can never fail the user's action.** Persistence problems are logged
  loudly and swallowed; a broken audit table must not lock everyone out of
  signing in.
* **It never competes with request handling for connections.** A handler
  backed by a DB session already holds a pool connection when it audits, so
  the writer uses its own small engine pool rather than borrowing a second
  connection from the request pool.

Secrets never reach the log: call sites record only field *names* for
credential changes, and :func:`_redact` drops the value of any
credential-named key as defence in depth for future call sites.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models.audit import AuditLog
from app.request_context import client_ip

_logger = logging.getLogger(__name__)

# Own pool: short single-connection writes, kept apart from the request
# pool so an audited request never needs two connections from it.
_engine = create_async_engine(
    get_settings().DATABASE_URL,
    pool_size=2,
    max_overflow=4,
    pool_pre_ping=True,
)
async_session_maker = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)

# Column-sized caps — anything longer is truncated rather than rejected,
# because user-supplied strings (an attempted username, a lab name) must
# not be able to make the write fail.
_MAX_LEN = {
    "actor": 64,
    "target_id": 128,
    "target_name": 255,
    "lab_name": 255,
    "ip": 64,
}
_MAX_DETAIL_CHARS = 16000
_LAB_PATH_MAX_CHARS = 2000

# A dict key equal to one of these, or ending in ``_<these>``, holds a
# credential: its value is replaced before the detail is serialized.
_CREDENTIAL_KEY_SUFFIXES = (
    "password",
    "password_hash",
    "passwd",
    "secret",
    "token",
    "cookie",
    "credentials",
    "private_key",
    "api_key",
    "session_key",
    "authorization",
)
_REDACTED = "[redacted]"

_EVENT_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_LIKE_ESCAPE_RE = re.compile(r"[\\%_]")

DEFAULT_PAGE_SIZE = 200
MAX_PAGE_SIZE = 1000


# ---------------------------------------------------------------------------
# Event model and writer
# ---------------------------------------------------------------------------


@dataclass
class AuditEvent:
    """One audited action, before it becomes a row."""

    event: str
    actor: Optional[str] = None
    outcome: str = "success"
    target_type: Optional[str] = None
    target_id: Any = None
    target_name: Optional[str] = None
    lab_path: Optional[str] = None
    lab_name: Optional[str] = None
    detail: dict[str, Any] = field(default_factory=dict)


def _clean(value: Any, limit: int) -> Optional[str]:
    """Stringify, drop control characters, and cap the length."""
    if value is None:
        return None
    text = _CONTROL_CHARS_RE.sub(" ", str(value)).strip()
    return text[:limit] if text else None


def _is_credential_key(key: str) -> bool:
    lowered = key.lower()
    return any(
        lowered == suffix or lowered.endswith(f"_{suffix}")
        for suffix in _CREDENTIAL_KEY_SUFFIXES
    )


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (_REDACTED if _is_credential_key(str(key)) else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _serialize_detail(detail: Optional[dict[str, Any]]) -> Optional[str]:
    if not detail:
        return None
    try:
        text = json.dumps(_redact(detail), default=str)
    except (TypeError, ValueError):
        return json.dumps({"unserializable": True})
    if len(text) > _MAX_DETAIL_CHARS:
        return json.dumps({"truncated": True, "keys": sorted(str(k) for k in detail)[:50]})
    return text


def _to_row(entry: AuditEvent, *, created_at: int, ip: Optional[str]) -> AuditLog:
    """Build the ORM row for ``entry``. Raises ``ValueError`` on a malformed
    event name — a programming error that must surface in tests, not be
    swallowed by the writer's best-effort handling."""
    if not _EVENT_RE.match(entry.event or ""):
        raise ValueError(
            f"audit event name must look like '<category>.<action>', got {entry.event!r}"
        )
    category, _, action = entry.event.partition(".")
    return AuditLog(
        created_at=created_at,
        actor=_clean(entry.actor, _MAX_LEN["actor"]),
        event=entry.event,
        category=category,
        action=action,
        outcome=_clean(entry.outcome, 16) or "success",
        target_type=_clean(entry.target_type, 32),
        target_id=_clean(entry.target_id, _MAX_LEN["target_id"]),
        target_name=_clean(entry.target_name, _MAX_LEN["target_name"]),
        lab_path=_clean(entry.lab_path, _LAB_PATH_MAX_CHARS),
        lab_name=_clean(entry.lab_name, _MAX_LEN["lab_name"]),
        ip=_clean(ip, _MAX_LEN["ip"]),
        detail=_serialize_detail(entry.detail),
    )


async def record_batch(entries: Sequence[AuditEvent]) -> None:
    """Persist ``entries`` in one transaction. Best-effort: never raises."""
    if not entries:
        return
    created_at = int(time.time())
    ip = client_ip()
    # Row construction stays outside the guard on purpose (see _to_row).
    rows = [_to_row(entry, created_at=created_at, ip=ip) for entry in entries]
    try:
        async with async_session_maker() as session:
            session.add_all(rows)
            await session.commit()
    except Exception:  # noqa: BLE001 — the trail must never fail the user's action
        _logger.exception(
            "audit: could not persist %d event(s): %s",
            len(rows),
            ", ".join(row.event for row in rows),
        )


async def record(event: str, **fields: Any) -> None:
    """Record one action. ``fields`` are the remaining :class:`AuditEvent` fields."""
    await record_batch([AuditEvent(event=event, **fields)])


# ---------------------------------------------------------------------------
# Context builders — turn lab JSON into the shared shape of a lab-scoped event
# ---------------------------------------------------------------------------


def display_path(path: Any) -> Optional[str]:
    """Lab/folder path in the leading-slash form the UI uses."""
    text = _clean(path, _LAB_PATH_MAX_CHARS)
    if not text:
        return None
    return text if text.startswith("/") else f"/{text}"


def field_changes(
    before: dict, after: dict, skip: Sequence[str] = ()
) -> dict[str, dict[str, Any]]:
    """``{field: {"from": old, "to": new}}`` for every field whose value
    differs — the shape every ``*.update`` event reports under ``changes``.

    A field missing from ``after`` is not reported: ``after`` may be a
    response payload that carries only the fields an endpoint returns."""
    return {
        field: {"from": old, "to": after[field]}
        for field, old in before.items()
        if field not in skip and field in after and after[field] != old
    }


def _mapping(data: Any, key: str) -> dict:
    if not isinstance(data, dict):
        return {}
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _by_id(data: Any, key: str, item_id: Any) -> dict:
    item = _mapping(data, key).get(str(item_id))
    return item if isinstance(item, dict) else {}


def _record(data: Any, key: str, item: Any) -> dict:
    """``item`` is either the record itself or its id in ``data[key]``.

    Creation handlers read the lab before the new object exists, so they
    pass the record they just built; everything else passes an id.
    """
    if isinstance(item, dict):
        return item
    return _by_id(data, key, item)


def lab_context(lab_data: Any = None, lab_path: Any = None, detail: Optional[dict] = None) -> dict:
    meta = _mapping(lab_data, "meta")
    return {
        "lab_path": display_path(lab_path),
        "lab_name": meta.get("name"),
        "detail": dict(detail or {}),
    }


def node_context(
    lab_data: Any, node: Any, lab_path: Any = None, detail: Optional[dict] = None
) -> dict:
    """Context for a node event: ``node`` is the node id or the node record.
    Pass an id only while the node still exists in ``lab_data``."""
    record = _record(lab_data, "nodes", node)
    context = lab_context(lab_data, lab_path)
    context.update(
        target_type="node",
        target_id=record.get("id", node if not isinstance(node, dict) else None),
        target_name=record.get("name"),
        detail={
            "node_type": record.get("type"),
            "template": record.get("template"),
            **(detail or {}),
        },
    )
    return context


def network_context(
    lab_data: Any, network: Any, lab_path: Any = None, detail: Optional[dict] = None
) -> dict:
    """Context for a network event: ``network`` is its id or its record."""
    record = _record(lab_data, "networks", network)
    context = lab_context(lab_data, lab_path)
    context.update(
        target_type="network",
        target_id=record.get("id", network if not isinstance(network, dict) else None),
        target_name=record.get("name"),
        detail={"network_type": record.get("type"), **(detail or {})},
    )
    return context


def _endpoint_label(lab_data: Any, endpoint: Any) -> str:
    if not isinstance(endpoint, dict):
        return "?"
    network_id = endpoint.get("network_id")
    if network_id is not None:
        network = _by_id(lab_data, "networks", network_id)
        return str(network.get("name") or f"network {network_id}")
    node_id = endpoint.get("node_id")
    node = _by_id(lab_data, "nodes", node_id)
    node_label = str(node.get("name") or f"node {node_id}")
    index = endpoint.get("interface_index")
    for interface in node.get("interfaces") or []:
        if isinstance(interface, dict) and interface.get("index") == index:
            return f"{node_label}:{interface.get('name') or f'eth{index}'}"
    return f"{node_label}:eth{index}"


def link_context(
    lab_data: Any, link: Any, lab_path: Any = None, detail: Optional[dict] = None
) -> dict:
    """Context for a link event. ``link`` is the stored link record."""
    record = link if isinstance(link, dict) else {}
    source = _endpoint_label(lab_data, record.get("from"))
    destination = _endpoint_label(lab_data, record.get("to"))
    context = lab_context(lab_data, lab_path)
    context.update(
        target_type="link",
        target_id=record.get("id"),
        target_name=f"{source} ↔ {destination}",
        detail={"from": source, "to": destination, **(detail or {})},
    )
    return context


def text_lengths(
    changes: dict[str, dict[str, Any]], fields: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """Replace the values of free-text ``fields`` with their lengths.

    That a lab's description or body changed is worth knowing; storing the
    text itself is not.
    """

    def length(value: Any) -> Optional[str]:
        if value is None or value == "":
            return None
        return f"{len(str(value))} characters"

    return {
        field: (
            {"from": length(change.get("from")), "to": length(change.get("to"))}
            if field in fields
            else change
        )
        for field, change in changes.items()
    }


# ---------------------------------------------------------------------------
# Console sessions (#300) — pairing a console's close with its open
# ---------------------------------------------------------------------------

# (lab path, node id, console mode, username)
ConsoleKey = tuple[str, int, str, str]

# A console whose close was never reported (crashed browser, lost
# connection) must not linger and pair with a much later close.
_CONSOLE_OPEN_MAX_AGE = 12 * 3600


class _OpenConsoles:
    """When each in-browser console was opened.

    In memory, per process. It only decorates a close with a duration, so
    losing it (a restart, more than one worker) costs the duration and
    nothing else.
    """

    def __init__(self) -> None:
        self._opened: dict[ConsoleKey, float] = {}

    def opened(self, key: ConsoleKey) -> None:
        self._prune()
        self._opened[key] = time.time()

    def closed(self, key: ConsoleKey) -> Optional[float]:
        """Seconds the console was open, or None if its open is unknown."""
        self._prune()
        started = self._opened.pop(key, None)
        return None if started is None else max(0.0, time.time() - started)

    def _prune(self) -> None:
        cutoff = time.time() - _CONSOLE_OPEN_MAX_AGE
        for key in [key for key, started in self._opened.items() if started < cutoff]:
            del self._opened[key]


_open_consoles = _OpenConsoles()


def console_key(lab_path: Any, node_id: Any, console: str, actor: Any) -> ConsoleKey:
    return (display_path(lab_path) or "", int(node_id), console, _clean(actor, _MAX_LEN["actor"]) or "")


async def record_console_open(
    *,
    actor: Any,
    lab_data: Any,
    node_id: Any,
    lab_path: Any = None,
    console: str = "primary",
    via: str = "html5",
    protocol: Any = None,
) -> None:
    """A console session began.

    ``via="html5"`` is an in-browser console: its tab will report the
    close, so it is registered to time it. ``external`` and ``native`` hand
    the console to a client that connects straight to the node — the server
    never sees that session end.
    """
    context = node_context(
        lab_data,
        node_id,
        lab_path,
        detail={"console_mode": console, "protocol": protocol, "via": via},
    )
    if via == "html5":
        _open_consoles.opened(console_key(lab_path, node_id, console, actor))
    await record("console.open", actor=actor, **context)


async def record_console_close(
    *,
    actor: Any,
    lab_data: Any,
    node_id: Any,
    lab_path: Any = None,
    console: str = "primary",
) -> Optional[float]:
    """An in-browser console session ended.

    Returns how long it was open, in whole seconds, or None if its open is
    unknown — the same number the entry carries.
    """
    seconds = _open_consoles.closed(console_key(lab_path, node_id, console, actor))
    duration = None if seconds is None else round(seconds)
    await record(
        "console.close",
        actor=actor,
        **node_context(
            lab_data,
            node_id,
            lab_path,
            detail={"console_mode": console, "duration": duration},
        ),
    )
    return duration


# ---------------------------------------------------------------------------
# Reader — the searchable view behind System → Logs → Activity log
# ---------------------------------------------------------------------------


def _escape_like(value: str) -> str:
    return _LIKE_ESCAPE_RE.sub(r"\\\g<0>", value)


def _filter_conditions(
    *,
    q: str = "",
    category: str = "",
    action: str = "",
    actor: str = "",
    since: Optional[int] = None,
    until: Optional[int] = None,
) -> list:
    conditions = []
    needle = q.strip()
    if needle:
        pattern = f"%{_escape_like(needle)}%"
        searchable = (
            AuditLog.actor,
            AuditLog.event,
            AuditLog.target_id,
            AuditLog.target_name,
            AuditLog.lab_name,
            AuditLog.lab_path,
            AuditLog.ip,
            AuditLog.detail,
        )
        conditions.append(
            or_(*[column.ilike(pattern, escape="\\") for column in searchable])
        )
    if category:
        conditions.append(AuditLog.category == category)
    if action:
        conditions.append(AuditLog.action == action)
    if actor:
        conditions.append(AuditLog.actor == actor)
    if since is not None:
        conditions.append(AuditLog.created_at >= int(since))
    if until is not None:
        conditions.append(AuditLog.created_at <= int(until))
    return conditions


def serialize_entry(row: AuditLog) -> dict[str, Any]:
    try:
        detail = json.loads(row.detail) if row.detail else None
    except (TypeError, ValueError):
        detail = {"unparseable": True}
    return {
        "id": row.id,
        "time": row.created_at,
        "actor": row.actor,
        "event": row.event,
        "category": row.category,
        "action": row.action,
        "outcome": row.outcome,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "target_name": row.target_name,
        "lab_path": row.lab_path,
        "lab_name": row.lab_name,
        "ip": row.ip,
        "detail": detail,
    }


async def list_entries(
    db: AsyncSession,
    *,
    q: str = "",
    category: str = "",
    action: str = "",
    actor: str = "",
    since: Optional[int] = None,
    until: Optional[int] = None,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> dict[str, Any]:
    """One page of the activity log, newest first, plus the total that
    matches the filters and the actors available for the filter dropdown."""
    conditions = _filter_conditions(
        q=q, category=category, action=action, actor=actor, since=since, until=until
    )

    total = (
        await db.execute(select(func.count()).select_from(AuditLog).where(*conditions))
    ).scalar()

    page = await db.execute(
        select(AuditLog)
        .where(*conditions)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(int(limit))
        .offset(int(offset))
    )

    actors = (
        await db.execute(
            select(AuditLog.actor)
            .where(AuditLog.actor.is_not(None))
            .distinct()
            .order_by(AuditLog.actor)
        )
    ).scalars().all()

    return {
        "entries": [serialize_entry(row) for row in page.scalars().all()],
        "total": int(total or 0),
        "limit": int(limit),
        "offset": int(offset),
        "actors": [actor for actor in actors if actor],
    }
