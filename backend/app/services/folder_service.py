import json
import asyncio
import logging
import os
import shutil
import stat
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

from app.config import get_settings
from app.services.cloud_inventory_service import nat_cloud_ipam_lock_async
from app.services.console_recording import RECORDINGS_FOLDER, get_console_recorder
from app.services.lab_lock import folder_namespace_lock, folder_namespace_lock_async
from app.services.lab_paths import FOLDER_DELETIONS


logger = logging.getLogger(__name__)

# Static virtual folders that always appear at root. Shared moved out of the
# pure-stub set in #285: it is a real directory, listed by the file scan once
# it exists and synthesized for every user's root listing until then.
STATIC_FOLDERS = ["Running", "Shared", "Users"]


def _shared_folder_entry() -> dict:
    return {
        "name": "Shared",
        "path": "/Shared",
        "umtime": int(time.time()),
        "mtime": _fmt_time(time.time()),
        "spy": -1,
        "lock": False,
        "shared": 0,
    }


def _labs_dir() -> Path:
    return get_settings().LABS_DIR.resolve()


def _normalize_folder_path(folder_path: str) -> str:
    clean = folder_path.strip().replace("\\", "/").strip("/")
    if clean in {"", "."}:
        return ""
    parts = [part for part in clean.split("/") if part not in {"", "."}]
    if any(part in {"..", RECORDINGS_FOLDER, FOLDER_DELETIONS} for part in parts):
        raise ValueError("Invalid folder path: directory traversal detected")
    return "/".join(parts)


def _safe_folder_path(folder_path: str, *, for_mutation: bool = False) -> Path:
    """Resolve a folder path within LABS_DIR. Prevents directory traversal."""
    labs_dir = _labs_dir()
    clean = _normalize_folder_path(folder_path)
    requested = labs_dir / clean
    target = requested.resolve()
    if not target.is_relative_to(labs_dir):
        raise ValueError("Invalid folder path: outside labs directory")
    if for_mutation and target != requested:
        raise ValueError("Folder changes require a canonical path.")
    return target


def _relative_folder_path(full_path: Path) -> str:
    """Get the API-style path (e.g., /my-folder) from a full filesystem path."""
    labs_dir = _labs_dir()
    try:
        rel = full_path.relative_to(labs_dir)
        return "/" + str(rel).replace(os.sep, "/")
    except ValueError:
        return "/"


def _fmt_time(ts: float) -> str:
    return time.strftime("%d %b %Y %H:%M", time.localtime(ts))


@asynccontextmanager
async def folder_mutation_lock():
    settings = get_settings()
    async with nat_cloud_ipam_lock_async(settings), folder_namespace_lock_async(settings.LABS_DIR):
        yield


class FolderService:
    @staticmethod
    def list_folder(folder_path: str = "", recursive: bool = False, at_user_root: bool = False) -> dict:
        """List contents of a folder. Returns folders and labs.

        ``at_user_root`` marks a folder-scoped user's own root listing so the
        Shared space is surfaced there too (#285).
        """
        labs_dir = _labs_dir()

        if folder_path == "" or folder_path == "/":
            target_dir = labs_dir
            is_root = True
        else:
            target_dir = _safe_folder_path(folder_path)
            is_root = False

        # Lazy-create the Shared space on first use (entering it directly),
        # so hosts provisioned before #285 grow it without re-provisioning.
        normalized = _normalize_folder_path(folder_path)
        if normalized == "Shared":
            with folder_namespace_lock(labs_dir, shared=True):
                target_dir = _safe_folder_path(normalized)
                target_dir.mkdir(parents=True, exist_ok=True)

        folders = []
        labs = []

        if target_dir.exists() and target_dir.is_dir():
            try:
                entries = sorted(target_dir.rglob("*")) if recursive else sorted(target_dir.iterdir())
            except (FileNotFoundError, NotADirectoryError):
                entries = []
            for entry in entries:
                if any(part in {RECORDINGS_FOLDER, FOLDER_DELETIONS} for part in entry.relative_to(target_dir).parts):
                    continue
                try:
                    st = entry.stat()
                except (FileNotFoundError, NotADirectoryError):
                    continue
                if stat.S_ISDIR(st.st_mode) and entry.name not in ("__pycache__", ".git"):
                    folders.append({
                        "name": entry.name,
                        "path": _relative_folder_path(entry),
                        "umtime": int(st.st_mtime),
                        "mtime": _fmt_time(st.st_mtime),
                        "spy": -1,
                        "lock": False,
                        "shared": 0,
                    })
                elif stat.S_ISREG(st.st_mode) and entry.suffix == ".json" and not entry.name.endswith(".v1.bak.json"):
                    rel = _relative_folder_path(entry)
                    labs.append({
                        "file": entry.name,
                        "path": rel,
                        "umtime": int(st.st_mtime),
                        "mtime": _fmt_time(st.st_mtime),
                        "spy": -1,
                        "lock": False,
                        "shared": 0,
                    })

        # Static folders at the labs root, listed ahead of the scan results
        # as before. Shared is skipped when the scan already listed the real
        # directory (dedupe by path, #285).
        if is_root:
            scanned_paths = {folder["path"] for folder in folders}
            stubs = []
            for name in STATIC_FOLDERS:
                if name == "Shared":
                    if "/Shared" not in scanned_paths:
                        stubs.append(_shared_folder_entry())
                else:
                    stubs.append({
                        "name": name,
                        "path": f"/{name}",
                        "umtime": int(time.time()),
                        "mtime": _fmt_time(time.time()),
                        "spy": -1,
                        "lock": False,
                        "shared": 0,
                    })
            folders = stubs + folders

        # A folder-scoped user's root is their own folder, not the labs
        # root — surface the shared space ahead of their folders (#285).
        if at_user_root and not is_root and "/Shared" not in {f["path"] for f in folders}:
            folders = [_shared_folder_entry()] + folders

        return {"folders": folders, "labs": labs}

    @staticmethod
    def annotate_running(labs: list[dict], lab_ids: dict[str, str]) -> None:
        """Set ``nodes_running`` on every listed lab (#365).

        The runtime registry is the source of truth, keyed by lab id, so no
        lab file is parsed unless a running lab has no DB row mapping its id
        to a path (a lab written straight to disk, e.g. an imported one).
        Only that fallback touches the disk; keep it off the event loop.

        ``lab_ids`` maps a listed lab path to its ``labs`` row id.
        """
        from app.services.lab_service import _lab_file_path
        from app.services.node_runtime_service import NodeRuntimeService

        running = NodeRuntimeService().running_nodes_by_lab()
        resolved: dict[str, int] = {}
        for lab in labs:
            count = running.pop(lab_ids.get(lab["path"]), None)
            if count:
                resolved[lab["path"]] = count
        if running:
            for lab in labs:
                if lab["path"] in lab_ids:
                    continue
                try:
                    data = json.loads(_lab_file_path(lab["path"]).read_text())
                except (OSError, ValueError):
                    continue
                lab_id = str(data.get("id", "")).strip() if isinstance(data, dict) else ""
                count = running.pop(lab_id, None)
                if count:
                    resolved[lab["path"]] = count
                if not running:
                    break
        for lab in labs:
            lab["nodes_running"] = resolved.get(lab["path"], 0)

    @staticmethod
    def create_folder(folder_path: str) -> Path:
        """Create a new folder."""
        with folder_namespace_lock(_labs_dir(), shared=True):
            target = _safe_folder_path(folder_path, for_mutation=True)
            if target.exists():
                raise FileExistsError(f"Folder already exists: {folder_path}")
            target.mkdir(parents=True, exist_ok=False)
            return target

    @staticmethod
    def create_folder_path(base_path: str, name: str | None) -> Path:
        normalized_base_path = _normalize_folder_path(base_path)
        normalized_name = _normalize_folder_path(name or "")
        if normalized_name and "/" in normalized_name:
            raise ValueError("Folder name must not contain path separators")
        target_path = "/".join(
            part for part in [normalized_base_path, normalized_name] if part
        )
        return FolderService.create_folder(target_path)

    @staticmethod
    def rename_folder(old_path: str, new_path: str) -> Path:
        """Rename a folder."""
        source = _safe_folder_path(old_path, for_mutation=True)
        dest = _safe_folder_path(new_path, for_mutation=True)
        if _labs_dir() in (source, dest):
            raise ValueError("The labs root cannot be renamed.")
        if not source.exists():
            raise FileNotFoundError(f"Folder not found: {old_path}")
        if not source.is_dir():
            raise NotADirectoryError(f"Not a folder: {old_path}")
        if dest.exists():
            raise FileExistsError(f"Folder already exists: {new_path}")
        if dest.is_relative_to(source):
            raise ValueError("A folder cannot be moved into itself.")
        recorder = get_console_recorder()
        shutil.move(str(source), str(dest))
        try:
            recorder.relocate_folder(source, dest)
        except BaseException:
            shutil.move(str(dest), str(source))
            recorder.relocate_folder(dest, source)
            raise
        return dest

    @staticmethod
    @asynccontextmanager
    async def delete_folder(folder_path: str):
        """Hide a folder during deletion and restore it if the DB change fails."""
        target = _safe_folder_path(folder_path, for_mutation=True)
        if target == _labs_dir():
            raise ValueError("The labs root cannot be deleted.")
        if not target.exists():
            raise FileNotFoundError(f"Folder not found: {folder_path}")
        if not target.is_dir():
            raise NotADirectoryError(f"Not a folder: {folder_path}")
        staging_root = _labs_dir() / FOLDER_DELETIONS
        staging_root.mkdir(mode=0o700, exist_ok=True)
        if staging_root.is_symlink():
            raise ValueError("Invalid folder deletion staging directory.")
        stage = Path(tempfile.mkdtemp(prefix="folder-", dir=staging_root))
        staged_folder = stage / "contents"
        try:
            target.rename(staged_folder)
        except OSError:
            stage.rmdir()
            raise

        recorder = get_console_recorder()
        try:
            recorder.relocate_folder(target, staged_folder)
            yield
        except BaseException:
            staged_folder.rename(target)
            recorder.relocate_folder(staged_folder, target)
            stage.rmdir()
            raise
        else:
            try:
                await asyncio.to_thread(shutil.rmtree, stage)
            except OSError:
                logger.warning("Could not remove staged folder %s", stage.name, exc_info=True)
