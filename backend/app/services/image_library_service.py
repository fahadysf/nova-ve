# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Image & template library service (#264).

Unifies the on-box image inventory with the template catalog:

* every filesystem image (qemu/iol/dynamips) plus the marked docker set,
  with human-facing size/mtime and a **lazily computed, cached sha256**
  (multi-GB disks make eager hashing prohibitive — ``verify_checksum``
  recomputes and persists one image on demand);
* bidirectional template↔image matching (mirrors the node-creation
  picker's ``_matches_template_image_name`` rule) so each template card
  can list its on-box images and each image row can list the templates
  that resolve to it;
* lab references: which labs' nodes point at each image, used to guard
  deletion.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

from app.config import get_settings
from app.services.template_service import TemplateError, TemplateService, _matches_template_image_name

FILESYSTEM_IMAGE_TYPES = ("qemu", "iol", "dynamips")
CHECKSUM_CACHE_FILENAME = "image-checksums.json"
_HASH_CHUNK_BYTES = 8 * 1024 * 1024

TEMPLATE_EDITABLE_FIELDS = {
    "name",
    "icon_type",
    "cpu",
    "ram",
    "ethernet",
    "console_type",
    "cpulimit",
    "image_aliases",
}

_TEMPLATE_INT_FIELDS = {"cpu", "ram", "ethernet", "cpulimit"}


class ImageLibraryError(Exception):
    pass


class ImageNotFound(ImageLibraryError):
    pass


class ImageReferenced(ImageLibraryError):
    def __init__(self, message: str, references: list[dict[str, Any]]):
        super().__init__(message)
        self.references = references


class TemplateUpdateInvalid(ImageLibraryError):
    pass


def _primary_file(path: Path) -> Path | None:
    """The file whose checksum represents the image: the only file for a
    flat file entry, the largest child for an image directory (the disk)."""
    if path.is_file():
        return path
    if path.is_dir():
        files = [child for child in path.iterdir() if child.is_file()]
        if not files:
            return None
        return max(files, key=lambda child: child.stat().st_size)
    return None


class ImageLibraryService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.images_dir = self.settings.IMAGES_DIR
        self.labs_dir = self.settings.LABS_DIR

    # ------------------------------------------------------------------
    # sha256 cache (path -> {size, mtime_ns, sha256})
    # ------------------------------------------------------------------
    def _cache_path(self) -> Path:
        return self.settings.TMP_DIR / CHECKSUM_CACHE_FILENAME

    def _load_cache(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self._cache_path().read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save_cache(self, cache: dict[str, dict[str, Any]]) -> None:
        path = self._cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, indent=1))

    def _cache_entry_valid(self, entry: Any, stat: Any) -> bool:
        return (
            isinstance(entry, dict)
            and entry.get("size") == stat.st_size
            and entry.get("mtime_ns") == stat.st_mtime_ns
            and isinstance(entry.get("sha256"), str)
        )

    def cached_checksum(self, path: Path) -> str | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        entry = self._load_cache().get(str(path))
        return entry["sha256"] if self._cache_entry_valid(entry, stat) else None

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while chunk := handle.read(_HASH_CHUNK_BYTES):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    async def verify_checksum(self, path: Path | str) -> dict[str, Any]:
        """Force-recompute the sha256 of the image's primary file.

        Runs in the default executor — hashing multi-GB disks must not
        block the event loop. The result is persisted keyed by
        (path, size, mtime_ns) so later requests reuse it.
        """
        path = Path(path)
        target = _primary_file(path)
        if target is None:
            raise ImageNotFound(f"No file to hash under {path}.")
        import asyncio

        sha256, size = await asyncio.get_running_loop().run_in_executor(
            None, self._hash_file, target
        )
        try:
            stat = target.stat()
            cache = self._load_cache()
            cache[str(target)] = {
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": sha256,
            }
            self._save_cache(cache)
        except OSError:
            pass
        return {"file": target.name, "sha256": sha256, "size": size}

    # ------------------------------------------------------------------
    # Inventory
    # ------------------------------------------------------------------
    def _scan_filesystem_images(self) -> list[dict[str, Any]]:
        cache = self._load_cache()
        images: list[dict[str, Any]] = []
        for image_type in FILESYSTEM_IMAGE_TYPES:
            type_root = self.images_dir / image_type
            if not type_root.exists():
                continue
            for child in sorted(type_root.iterdir()):
                if child.is_dir():
                    files = sorted(f.name for f in child.iterdir() if f.is_file())
                    size = sum(f.stat().st_size for f in child.iterdir() if f.is_file())
                    mtime = max((f.stat().st_mtime for f in child.iterdir() if f.is_file()), default=None)
                elif child.is_file():
                    files = [child.name]
                    size = child.stat().st_size
                    mtime = child.stat().st_mtime
                else:
                    continue
                primary = _primary_file(child)
                sha256 = None
                if primary is not None:
                    entry = cache.get(str(primary))
                    if entry is not None and self._cache_entry_valid(entry, primary.stat()):
                        sha256 = entry["sha256"]
                images.append(
                    {
                        "name": child.name if child.is_dir() else child.stem,
                        "filename": child.name,
                        "type": image_type,
                        "path": str(child),
                        "files": files,
                        "size": size,
                        "mtime": mtime,
                        "sha256": sha256,
                        "state": "available" if files else "missing",
                    }
                )
        return images

    def _scan_docker_images(self) -> list[dict[str, Any]]:
        from app.services.docker_image_service import DockerImageService

        service = DockerImageService()
        images: list[dict[str, Any]] = []
        for image_name in service.list_marked_image_names():
            images.append(
                {
                    "name": image_name,
                    "filename": image_name,
                    "type": "docker",
                    "path": image_name,
                    "files": [],
                    "size": None,
                    "mtime": None,
                    "sha256": None,
                    "state": "available",
                }
            )
        return images

    def _scan_references(self) -> dict[str, list[dict[str, Any]]]:
        """Map image name -> referencing labs (from lab.json nodes)."""
        references: dict[str, list[dict[str, Any]]] = {}
        if not self.labs_dir.exists():
            return references
        for lab_file in sorted(self.labs_dir.rglob("*.json")):
            try:
                data = json.loads(lab_file.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            nodes = data.get("nodes")
            if not isinstance(nodes, dict):
                continue
            try:
                lab_path = f"/{lab_file.relative_to(self.labs_dir).as_posix()}"
            except ValueError:
                continue
            lab_name = str((data.get("meta") or {}).get("name") or lab_file.stem)
            for node in nodes.values():
                if not isinstance(node, dict):
                    continue
                image_name = str(node.get("image") or "").strip()
                if not image_name:
                    continue
                references.setdefault(image_name, []).append(
                    {
                        "lab_path": lab_path,
                        "lab": lab_file.name,
                        "lab_name": lab_name,
                        "node": str(node.get("name") or node.get("id") or ""),
                        "template": str(node.get("template") or ""),
                    }
                )
        return references

    @staticmethod
    def _template_matches_image(template: dict[str, Any], image: dict[str, Any]) -> bool:
        if template.get("type") != image["type"]:
            return False
        if image["type"] == "docker":
            # Docker templates resolve through the marked catalog by name.
            candidates = [template.get("name", ""), *template.get("image_aliases", [])]
            lowered = [c.strip().lower() for c in candidates if c]
            if not lowered:
                return True
            image_name = image["name"].lower()
            return any(c in image_name or image_name in c for c in lowered)
        # Filesystem types use the same alias/containment rule as the
        # node-creation picker so the library agrees with what actually
        # appears in the Add-node modal.
        shim = SimpleNamespace(name=template.get("name", ""), image_aliases=list(template.get("image_aliases", [])))
        return _matches_template_image_name(shim, image["name"])

    def inventory(self) -> dict[str, Any]:
        images = self._scan_filesystem_images() + self._scan_docker_images()
        references = self._scan_references()

        template_service = TemplateService()
        templates: list[dict[str, Any]] = []
        for template in template_service._load_templates():
            entry = template.as_response()
            matched = [image for image in images if self._template_matches_image(entry, image)]
            entry["images"] = [
                {
                    "name": image["name"],
                    "filename": image["filename"],
                    "state": image["state"],
                    "size": image["size"],
                }
                for image in matched
            ]
            templates.append(entry)

        for image in images:
            image["referencedBy"] = references.get(image["name"], [])
            image["templates"] = [
                {"type": t["type"], "key": t["id"], "name": t["name"]}
                for t in templates
                if any(m["name"] == image["name"] for m in t["images"])
            ]

        return {
            "templates": templates,
            "images": images,
        }

    # ------------------------------------------------------------------
    # Template edits
    # ------------------------------------------------------------------
    def update_template(
        self, template_type: str, key: str, updates: dict[str, Any]
    ) -> dict[str, Any]:
        """Persist editable template fields.

        User-dir templates are edited in place; builtin templates get a
        shadow YAML under USER_TEMPLATES_DIR (the loader prefers user-dir
        entries on key collision, so the edit wins without touching the
        shipped template files).
        """
        template_service = TemplateService()
        template = template_service.get_template(template_type, key)
        if template.paired_parent is not None:
            raise TemplateUpdateInvalid(
                "Paired-template children are managed through their parent template."
            )

        clean: dict[str, Any] = {}
        for field, value in updates.items():
            if field not in TEMPLATE_EDITABLE_FIELDS:
                continue
            if field in _TEMPLATE_INT_FIELDS:
                try:
                    int_value = int(value)
                except (TypeError, ValueError):
                    raise TemplateUpdateInvalid(f"{field} must be an integer.")
                if int_value < 1:
                    raise TemplateUpdateInvalid(f"{field} must be at least 1.")
                clean[field] = int_value
            elif field == "image_aliases":
                if not isinstance(value, list) or not all(
                    isinstance(item, str) for item in value
                ):
                    raise TemplateUpdateInvalid("image_aliases must be a list of strings.")
                clean[field] = [item.strip() for item in value if item.strip()]
            else:
                if not isinstance(value, str) or not value.strip():
                    raise TemplateUpdateInvalid(f"{field} must be a non-empty string.")
                clean[field] = value.strip()
        if not clean:
            raise TemplateUpdateInvalid("No editable fields supplied.")

        user_dir = template_service.user_templates_dir
        if user_dir is None:
            raise ImageLibraryError("USER_TEMPLATES_DIR is not configured.")
        target = user_dir / f"{key}.yml"

        if target.exists():
            # Edit the existing user template in place.
            try:
                payload = yaml.safe_load(target.read_text()) or {}
            except yaml.YAMLError as exc:
                raise TemplateUpdateInvalid(f"Existing template YAML is invalid: {exc}")
            if not isinstance(payload, dict):
                payload = {}
        else:
            # Shadow a builtin: start from its resolved raw payload so all
            # non-edited fields (type, extras, interface_naming, ...) carry
            # over verbatim.
            payload = dict(template.raw)

        payload.update(clean)
        payload.setdefault("type", template.type)

        try:
            yaml.safe_dump(payload, target.open("w"), sort_keys=False)
        except OSError as exc:
            raise ImageLibraryError(f"Failed to write template: {exc}")

        # Re-validate through a fresh load — this runs the same
        # interface_naming/capabilities validation the runtime applies.
        try:
            reloaded = TemplateService().get_template(template_type, key)
        except TemplateError as exc:
            raise TemplateUpdateInvalid(f"Edit rejected: {exc}")
        return reloaded.as_response()

    # ------------------------------------------------------------------
    # Image deletion
    # ------------------------------------------------------------------
    def delete_image(self, image_type: str, name: str) -> dict[str, Any]:
        if image_type == "docker":
            raise ImageLibraryError(
                "Docker images are managed by the docker daemon; unmark the image instead."
            )
        if image_type not in FILESYSTEM_IMAGE_TYPES:
            raise ImageLibraryError(f"Unsupported image type: {image_type}")

        # Name lookup matches the inventory scan: directories keep their
        # full name, flat files resolve by stem.
        target: Path | None = None
        type_root = self.images_dir / image_type
        if type_root.exists():
            for child in sorted(type_root.iterdir()):
                child_name = child.name if child.is_dir() else child.stem
                if child_name == name:
                    target = child
                    break
        if target is None:
            raise ImageNotFound(f"Image {name} ({image_type}) not found.")

        resolved = target.resolve()
        images_root = self.images_dir.resolve()
        if images_root not in resolved.parents:
            raise ImageLibraryError("Refusing to delete outside the images directory.")

        references = self._scan_references().get(name, [])
        if references:
            raise ImageReferenced(
                f"Image {name} is referenced by {len(references)} lab node(s); "
                "remove those nodes first.",
                references,
            )

        if target.is_dir():
            import shutil

            shutil.rmtree(target)
        else:
            target.unlink()
        return {"name": name, "type": image_type, "path": str(target)}
