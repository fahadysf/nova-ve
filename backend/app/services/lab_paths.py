import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.ws_hub import WsHub


FOLDER_DELETIONS = ".folder-deletions"


class LabMembershipPathError(PermissionError):
    """A lab mutation was requested through a noncanonical path."""


def iter_lab_json_files(root: Path) -> Iterator[Path]:
    """Scan lab files without exposing folders staged for database deletion."""
    return (
        path for path in root.rglob("*.json")
        if FOLDER_DELETIONS not in path.relative_to(root).parts
    )


def read_canonical_lab(root: Path, path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_relative_to(root) or path.resolve() != path:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RuntimeError):
        return None
    return data if isinstance(data, dict) else None


def lab_identity(data: dict[str, Any] | None) -> str | None:
    return str((data or {}).get("id") or "").strip() or None


def _runtime_lab_paths(root: Path) -> dict[str, str]:
    path_by_id: dict[str, str] = {}
    duplicates: set[str] = set()
    for path in iter_lab_json_files(root):
        lab_id = lab_identity(read_canonical_lab(root, path))
        if not lab_id or lab_id in duplicates:
            continue
        if lab_id in path_by_id:
            path_by_id.pop(lab_id)
            duplicates.add(lab_id)
        else:
            path_by_id[lab_id] = path.relative_to(root).as_posix()
    return path_by_id


@asynccontextmanager
async def _runtime_event_guard(root: Path, path: str, lab_id: str) -> AsyncIterator[bool]:
    from app.services.lab_lock import LabLockTimeout, lab_lock_async

    async with AsyncExitStack() as stack:
        try:
            await stack.enter_async_context(lab_lock_async(path, root, create_parent=False))
        except (OSError, LabLockTimeout, RuntimeError):
            yield False
            return
        data = await asyncio.to_thread(read_canonical_lab, root, root / path)
        yield lab_identity(data) == lab_id


async def runtime_lab_paths(labs_dir: Path) -> dict[str, str]:
    return await asyncio.to_thread(_runtime_lab_paths, labs_dir.resolve())


async def find_runtime_lab_path(labs_dir: Path, lab_id: str) -> str | None:
    return (await runtime_lab_paths(labs_dir)).get(lab_id)


async def publish_runtime_event(
    hub: "WsHub",
    labs_dir: Path,
    lab_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    path: str | None = None,
    rev: str = "",
) -> None:
    """Find the lab's subscription path and validate its identity before enqueueing."""
    if not lab_id:
        return
    if path is None:
        path = await find_runtime_lab_path(labs_dir, lab_id)
    if path is not None:
        await hub.publish(
            path, event_type, payload, rev=rev,
            identity=lab_id,
            guard=_runtime_event_guard(labs_dir.resolve(), path, lab_id),
        )
