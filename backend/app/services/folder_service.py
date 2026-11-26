import json
import os
import shutil
import time
from pathlib import Path

from app.config import get_settings
from app.services.console_recording import RECORDINGS_FOLDER, get_console_recorder

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
    if ".." in parts or RECORDINGS_FOLDER in parts:
        raise ValueError("Invalid folder path: directory traversal detected")
    return "/".join(parts)


def _safe_folder_path(folder_path: str) -> Path:
    """Resolve a folder path within LABS_DIR. Prevents directory traversal."""
    labs_dir = _labs_dir()
    clean = _normalize_folder_path(folder_path)
    target = (labs_dir / clean).resolve()
    if not str(target).startswith(str(labs_dir)):
        raise ValueError("Invalid folder path: outside labs directory")
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
        if normalized == "Shared" or normalized.startswith("Shared/"):
            target_dir.mkdir(parents=True, exist_ok=True)

        folders = []
        labs = []

        if target_dir.exists() and target_dir.is_dir():
            entries = sorted(target_dir.rglob("*")) if recursive else sorted(target_dir.iterdir())
            for entry in entries:
                if RECORDINGS_FOLDER in entry.relative_to(target_dir).parts:
                    continue
                if entry.is_dir() and entry.name not in ("__pycache__", ".git"):
                    st = entry.stat()
                    folders.append({
                        "name": entry.name,
                        "path": _relative_folder_path(entry),
                        "umtime": int(st.st_mtime),
                        "mtime": _fmt_time(st.st_mtime),
                        "spy": -1,
                        "lock": False,
                        "shared": 0,
                    })
                elif entry.is_file() and entry.suffix == ".json" and not entry.name.endswith(".v1.bak.json"):
                    st = entry.stat()
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

        The runtime registry is the source of truth — the same liveness
        check behind /api/manage/labs — keyed by lab id, so no lab file is
        parsed unless a running lab has no DB row mapping its id to a path
        (a lab written straight to disk, e.g. an imported one). Blocking
        (``/proc`` reads, docker/hypervisor queries): call it from a worker
        thread.

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
        target = _safe_folder_path(folder_path)
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
        source = _safe_folder_path(old_path)
        dest = _safe_folder_path(new_path)
        if not source.exists():
            raise FileNotFoundError(f"Folder not found: {old_path}")
        if dest.exists():
            raise FileExistsError(f"Folder already exists: {new_path}")
        shutil.move(str(source), str(dest))
        get_console_recorder().relocate_folder(source, dest)
        return dest

    @staticmethod
    def delete_folder(folder_path: str) -> None:
        """Delete a folder and all nested contents."""
        target = _safe_folder_path(folder_path)
        if not target.exists():
            raise FileNotFoundError(f"Folder not found: {folder_path}")
        if not target.is_dir():
            raise NotADirectoryError(f"Not a folder: {folder_path}")
        shutil.rmtree(target)
