import json
import uuid
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.lab import LabMeta
from app.config import get_settings

settings = get_settings()


def _lab_file_path(filename: str) -> Path:
    """Resolve a lab filename to an absolute path within LABS_DIR."""
    labs_dir = settings.LABS_DIR.resolve()
    # Strip any leading slashes or parent directory references
    safe_name = Path(filename).name
    filepath = (labs_dir / safe_name).resolve()
    # Security: ensure the resolved path is still within LABS_DIR
    if not str(filepath).startswith(str(labs_dir)):
        raise ValueError("Invalid lab path: directory traversal detected")
    return filepath


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
        result = await self.db.execute(
            select(LabMeta).where(LabMeta.filename == filename)
        )
        return result.scalar_one_or_none()

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
        # Default filename from name
        if not filename:
            safe_name = "".join(c if c.isalnum() or c in "-_" else "-" for c in name)
            filename = f"{safe_name}.json"
        # Ensure .json extension
        if not filename.endswith(".json"):
            filename += ".json"

        filepath = _lab_file_path(filename)
        if filepath.exists():
            raise FileExistsError(f"Lab file already exists: {filename}")

        # Create JSON file
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

        # Create DB record
        db_meta = {k: v for k, v in meta.items() if hasattr(LabMeta, k) and k != "name"}
        lab = LabMeta(
            id=lab_id,
            owner=owner,
            filename=filename,
            name=name,
            path=path or str(filepath),
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

        # Update DB fields
        db_fields = {"name", "author", "description", "body", "version",
                     "scripttimeout", "countdown", "linkwidth", "grid", "lock"}
        for field, value in updates.items():
            if field in db_fields and hasattr(lab, field) and value is not None:
                setattr(lab, field, value)

        await self.db.commit()
        await self.db.refresh(lab)

        # Update JSON file meta section
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
        """Delete lab: DB record + JSON file."""
        filepath = _lab_file_path(lab.filename)
        if filepath.exists():
            filepath.unlink()
        await self.db.delete(lab)
        await self.db.commit()

    def read_lab_json(self, filename: str) -> dict:
        """Read lab JSON from disk."""
        filepath = _lab_file_path(filename)
        with open(filepath, "r") as f:
            return json.load(f)
