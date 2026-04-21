import json
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.lab import LabMeta


def _labs_dir() -> Path:
    return get_settings().LABS_DIR.resolve()


def _normalize_relative_lab_path(raw_path: str) -> str:
    """Normalize a LABS_DIR-relative path and reject traversal."""
    candidate = raw_path.strip().replace("\\", "/").strip("/")
    parts = [part for part in candidate.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("Invalid lab path: directory traversal detected")
    return Path(*parts).as_posix()


def _lab_file_path(relative_path: str) -> Path:
    labs_dir = _labs_dir()
    normalized_relative_path = _normalize_relative_lab_path(relative_path)
    filepath = (labs_dir / normalized_relative_path).resolve()
    if not str(filepath).startswith(str(labs_dir)):
        raise ValueError("Invalid lab path: directory traversal detected")
    return filepath


def _safe_lab_filename(name: str) -> str:
    safe_name = "".join(c if c.isalnum() or c in "-_" else "-" for c in name).strip("-_")
    return safe_name or "lab"


def _api_lab_path(relative_path: str) -> str:
    return "/" + _normalize_relative_lab_path(relative_path)


def build_relative_lab_path(
    name: str,
    path: str | None = None,
    filename: str | None = None,
) -> str:
    """Build a relative lab path from API inputs."""
    if filename:
        relative_path = _normalize_relative_lab_path(filename)
        if not relative_path.endswith(".json"):
            relative_path += ".json"
        return relative_path

    generated_name = f"{_safe_lab_filename(name)}.json"
    if path:
        normalized_path = _normalize_relative_lab_path(path)
        if normalized_path.endswith(".json"):
            return normalized_path
        return f"{normalized_path}/{generated_name}"

    return generated_name


def _empty_lab_json(lab_id: str, meta: dict) -> dict:
    return {
        "id": lab_id,
        "meta": meta,
        "nodes": {},
        "networks": {},
        "topology": [],
        "textobjects": [],
        "lineobjects": [],
        "pictures": [],
        "tasks": [],
        "configsets": {},
    }


class LabService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_lab_by_filename(self, filename: str) -> LabMeta | None:
        normalized_filename = _normalize_relative_lab_path(filename)
        result = await self.db.execute(
            select(LabMeta).where(LabMeta.filename == normalized_filename)
        )
        return result.scalar_one_or_none()

    async def list_labs(self) -> list[LabMeta]:
        result = await self.db.execute(select(LabMeta))
        return list(result.scalars().all())

    async def create_lab(
        self,
        owner: str,
        name: str,
        path: str | None = None,
        filename: str | None = None,
        **meta_fields,
    ) -> LabMeta:
        """Create a new lab: DB record + JSON file."""
        lab_id = uuid.uuid4()
        relative_path = build_relative_lab_path(name=name, path=path, filename=filename)
        filepath = _lab_file_path(relative_path)
        if filepath.exists():
            raise FileExistsError(f"Lab file already exists: {relative_path}")

        meta = {
            "name": name,
            "author": meta_fields.get("author", ""),
            "description": meta_fields.get("description", ""),
            "body": meta_fields.get("body", ""),
            "version": meta_fields.get("version", "0"),
            "scripttimeout": meta_fields.get("scripttimeout", 300),
            "countdown": meta_fields.get("countdown", 0),
            "linkwidth": meta_fields.get("linkwidth", "1"),
            "grid": meta_fields.get("grid", True),
            "lock": meta_fields.get("lock", False),
            "sat": meta_fields.get("sat", "-1"),
        }
        lab_json = _empty_lab_json(str(lab_id), meta)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w") as f:
            json.dump(lab_json, f, indent=2)

        db_meta = {k: v for k, v in meta.items() if hasattr(LabMeta, k) and k != "name"}
        lab = LabMeta(
            id=lab_id,
            owner=owner,
            filename=relative_path,
            name=name,
            path=_api_lab_path(relative_path),
            **db_meta,
        )
        self.db.add(lab)
        await self.db.commit()
        await self.db.refresh(lab)
        return lab

    async def update_lab(
        self,
        lab: LabMeta,
        **updates,
    ) -> LabMeta:
        """Update lab metadata in both DB and JSON file."""
        filepath = _lab_file_path(lab.filename)
        db_fields = {
            "name",
            "author",
            "description",
            "body",
            "version",
            "scripttimeout",
            "countdown",
            "linkwidth",
            "grid",
            "lock",
        }
        for field, value in updates.items():
            if field in db_fields and hasattr(lab, field) and value is not None:
                setattr(lab, field, value)

        await self.db.commit()
        await self.db.refresh(lab)

        if filepath.exists():
            with open(filepath, "r") as f:
                data = json.load(f)
            for field in db_fields:
                if field in updates and updates[field] is not None:
                    data["meta"][field] = updates[field]
            with open(filepath, "w") as f:
                json.dump(data, f, indent=2)

        return lab

    async def delete_lab(self, lab: LabMeta) -> None:
        filepath = _lab_file_path(lab.filename)
        if filepath.exists():
            filepath.unlink()
        await self.db.delete(lab)
        await self.db.commit()

    def read_lab_json(self, filename: str) -> dict:
        return self.read_lab_json_static(filename)

    def write_lab_json(self, filename: str, data: dict) -> None:
        self.write_lab_json_static(filename, data)

    @staticmethod
    def read_lab_json_static(filename: str) -> dict:
        filepath = _lab_file_path(filename)
        with open(filepath, "r") as f:
            return json.load(f)

    @staticmethod
    def write_lab_json_static(filename: str, data: dict) -> None:
        filepath = _lab_file_path(filename)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
