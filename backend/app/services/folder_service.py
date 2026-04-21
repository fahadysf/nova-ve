import os
import shutil
import time
from pathlib import Path

from app.config import get_settings

# Static virtual folders that always appear at root
STATIC_FOLDERS = ["Running", "Shared", "Users"]


def _labs_dir() -> Path:
    return get_settings().LABS_DIR.resolve()


def _normalize_folder_path(folder_path: str) -> str:
    clean = folder_path.strip().replace("\\", "/").strip("/")
    if clean in {"", "."}:
        return ""
    parts = [part for part in clean.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
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
    def list_folder(folder_path: str = "") -> dict:
        """List contents of a folder. Returns folders and labs."""
        labs_dir = _labs_dir()

        if folder_path == "" or folder_path == "/":
            target_dir = labs_dir
            is_root = True
        else:
            target_dir = _safe_folder_path(folder_path)
            is_root = False

        folders = []
        labs = []

        # Static folders at root
        if is_root:
            for name in STATIC_FOLDERS:
                folders.append({
                    "name": name,
                    "path": f"/{name}",
                    "umtime": int(time.time()),
                    "mtime": _fmt_time(time.time()),
                    "spy": -1,
                    "lock": False,
                    "shared": 0,
                })

        if target_dir.exists() and target_dir.is_dir():
            for entry in sorted(target_dir.iterdir()):
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
                elif entry.is_file() and entry.suffix == ".json":
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

        return {"folders": folders, "labs": labs}

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
