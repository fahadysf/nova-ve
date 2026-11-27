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
import math
import os
import re
import shlex
from pathlib import Path
from stat import S_IMODE
from tempfile import NamedTemporaryFile
from threading import Lock
from types import SimpleNamespace
from typing import Any, Iterator

import yaml

from app.services.lab_paths import iter_lab_json_files
from app.config import get_settings
from app.schemas.node import validate_console_mode_for_type
from app.services.template_service import (
    ICON_TYPE_TO_FILENAME,
    TemplateDefinition,
    TemplateError,
    TemplateService,
    _extras_schema_for,
    _matches_template_image_name,
)

FILESYSTEM_IMAGE_TYPES = ("qemu", "iol", "dynamips")
CHECKSUM_CACHE_FILENAME = "image-checksums.json"
_HASH_CHUNK_BYTES = 8 * 1024 * 1024

TEMPLATE_EDITABLE_FIELDS = {
    "name",
    "description",
    "icon_type",
    "cpu",
    "ram",
    "ethernet",
    "console_type",
    "second_console",
    "cpulimit",
    "image_aliases",
    "interface_naming",
    "capabilities",
    "extras",
}

_TEMPLATE_INT_FIELDS = {"cpu", "ram", "ethernet", "cpulimit"}
_INSTANCE_EXTRA_FIELDS = {"uuid", "firstmac"}
_TEMPLATE_CAPABILITY_FIELDS = {"hotplug", "max_nics", "machine"}
_TEMPLATE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}")
MAX_TEMPLATE_YAML_BYTES = 256 * 1024
_template_edit_lock = Lock()


class ImageLibraryError(Exception):
    pass


class ImageNotFound(ImageLibraryError):
    pass


class ImageNameConflict(ImageLibraryError):
    pass


class ImageReferenced(ImageLibraryError):
    def __init__(self, message: str, references: list[dict[str, Any]]):
        super().__init__(message)
        self.references = references


class TemplateUpdateInvalid(ImageLibraryError):
    pass


class TemplateConflict(ImageLibraryError):
    pass


class TemplateReferenced(TemplateConflict):
    def __init__(self, key: str, references: list[dict[str, Any]]):
        super().__init__(f"Template {key} is used by {len(references)} lab node(s). Remove or change those nodes first.")
        self.references = references


def editable_template_extras_schema(
    template_type: str, extras: dict[str, Any]
) -> list[dict[str, Any]]:
    # CPU limit already has a top-level field. Dynamips platform identifies
    # the appliance and determines its slot schema. UUID and MAC are per-node.
    excluded = _INSTANCE_EXTRA_FIELDS | {"cpulimit", "platform"}
    return [field for field in _extras_schema_for(template_type, extras) if field["key"] not in excluded]


def _validate_engine_value(field: dict[str, Any], value: Any) -> None:
    key, kind = field["key"], field["type"]
    if kind == "select":
        if value not in [option["value"] for option in field["options"]]:
            raise TemplateUpdateInvalid(f"extras.{key} must be one of the available options.")
    elif kind == "number":
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise TemplateUpdateInvalid(f"extras.{key} must be a non-negative integer.")
        if key == "vnc_port" and not 1 <= value <= 65535:
            raise TemplateUpdateInvalid("extras.vnc_port must be between 1 and 65535.")
    elif kind == "env":
        if not isinstance(value, list) or not all(
            isinstance(entry, dict)
            and isinstance(entry.get("key"), str)
            and bool(entry["key"].strip())
            and "=" not in entry["key"]
            and "\0" not in entry["key"]
            and isinstance(entry.get("value"), str)
            and "\0" not in entry["value"]
            for entry in value
        ):
            raise TemplateUpdateInvalid(f"extras.{key} must contain valid environment names and string values.")
    elif key == "command" and isinstance(value, list):
        if not all(isinstance(argument, str) and "\0" not in argument for argument in value):
            raise TemplateUpdateInvalid("extras.command arguments must be strings.")
    else:
        if not isinstance(value, str) or "\0" in value:
            raise TemplateUpdateInvalid(f"extras.{key} must be a string.")
        if key in {"qemu_options", "extra_args", "command"}:
            try:
                shlex.split(value)
            except ValueError as exc:
                raise TemplateUpdateInvalid(f"Invalid extras.{key}: {exc}") from exc


def _patch_engine_extras(
    payload: dict[str, Any], template_type: str, configured_extras: dict[str, Any], edits: Any
) -> None:
    if not isinstance(edits, dict):
        raise TemplateUpdateInvalid("extras must be an object.")
    schema = {field["key"]: field for field in editable_template_extras_schema(template_type, configured_extras)}
    raw_extras = payload.get("extras")
    extras = dict(raw_extras) if isinstance(raw_extras, dict) else {}
    for key, value in edits.items():
        if key not in schema:
            raise TemplateUpdateInvalid(f"extras.{key} cannot be edited for {template_type} templates.")
        if value is None:
            # Runtime accepts both nested overrides and legacy top-level keys.
            extras.pop(key, None)
            payload.pop(key, None)
        else:
            _validate_engine_value(schema[key], value)
            if key in payload and key not in extras:
                payload[key] = value
            else:
                extras[key] = value
    if extras or isinstance(raw_extras, dict):
        payload["extras"] = extras


def _write_template_atomically(
    target: Path, payload: dict[str, Any], *, create: bool = False
) -> None:
    content = (
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if target.suffix == ".json"
        else yaml.safe_dump(payload, sort_keys=False)
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if create:
            os.link(temporary, target)
        else:
            if target.exists():
                temporary.chmod(S_IMODE(target.stat().st_mode))
            temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _validate_template_path(target: Path, template_service: TemplateService) -> None:
    user_dir = template_service.user_templates_dir
    if user_dir is None:
        raise ImageLibraryError("USER_TEMPLATES_DIR is not configured.")
    if (
        target.is_symlink()
        or not target.parent.resolve().is_relative_to(user_dir.resolve())
        or target.resolve().is_relative_to(template_service.templates_dir.resolve())
    ):
        raise TemplateUpdateInvalid("The template must be editable inside the user templates directory.")


def _read_template_yaml(content: bytes) -> dict[str, Any]:
    if len(content) > MAX_TEMPLATE_YAML_BYTES:
        raise TemplateUpdateInvalid("Template YAML files must be 256 KiB or smaller.")
    try:
        payload = yaml.safe_load(content)
    except (yaml.YAMLError, RecursionError, ValueError) as exc:
        raise TemplateUpdateInvalid("Invalid template YAML. Upload a single YAML document.") from exc
    if not isinstance(payload, dict):
        raise TemplateUpdateInvalid("Template YAML must contain a single object.")

    # Library responses include template values as JSON. Bound the expanded
    # structure as well as the file size, since YAML anchors can share nodes.
    pending = [(payload, 0)]
    remaining = 10_000
    while pending:
        value, depth = pending.pop()
        remaining -= 1
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise TemplateUpdateInvalid("Template YAML keys must be strings.")
            children = value.values()
        elif isinstance(value, list):
            children = value
        else:
            if (
                not isinstance(value, (str, int, float, bool, type(None)))
                or isinstance(value, float) and not math.isfinite(value)
            ):
                raise TemplateUpdateInvalid("Template YAML values must be JSON-compatible.")
            continue
        if depth >= 32 or len(children) + len(pending) > remaining:
            raise TemplateUpdateInvalid("Template YAML contains too much nested data.")
        pending.extend((child, depth + 1) for child in children)
    return payload


def _validate_new_template(
    service: TemplateService, template_type: str, target: Path, payload: dict[str, Any]
) -> TemplateDefinition:
    for field in ("name", "description", "icon_type", "console_type"):
        if field in payload and not isinstance(payload[field], str):
            raise TemplateUpdateInvalid(f"{field} must be a string.")
        if field in payload and field != "description" and not payload[field].strip():
            raise TemplateUpdateInvalid(f"{field} must not be blank.")
    if "extras" in payload and not isinstance(payload["extras"], dict):
        raise TemplateUpdateInvalid("extras must be an object.")

    for field in _TEMPLATE_INT_FIELDS:
        if field in payload and (
            isinstance(payload[field], bool) or not isinstance(payload[field], (int, str))
        ):
            raise TemplateUpdateInvalid(f"{field} must be an integer.")
    try:
        template = service._definition_from_payload(payload, target, template_type, user=True)
        validate_console_mode_for_type(template_type, template.console_type)
    except (TemplateError, ValueError, TypeError) as exc:
        raise TemplateUpdateInvalid(str(exc)) from exc

    for field in _TEMPLATE_INT_FIELDS:
        minimum = 0 if field == "cpulimit" else 1
        if getattr(template, field) < minimum:
            raise TemplateUpdateInvalid(f"{field} must be an integer of at least {minimum}.")
    return template


def _patched_template_payload(
    template: TemplateDefinition, updates: dict[str, Any]
) -> dict[str, Any]:
    updates = {field: value for field, value in updates.items() if field in TEMPLATE_EDITABLE_FIELDS}
    if not updates:
        raise TemplateUpdateInvalid("No editable fields supplied.")

    payload = dict(template.raw)
    for field, value in updates.items():
        if field == "extras":
            _patch_engine_extras(payload, template.type, template.extras, value)
            continue
        if field == "capabilities":
            if not isinstance(value, dict) or set(value) - _TEMPLATE_CAPABILITY_FIELDS:
                raise TemplateUpdateInvalid("capabilities must contain only hotplug, max_nics, or machine.")
            value = {**(payload.get(field) or {}), **value}
        elif field in _TEMPLATE_INT_FIELDS:
            minimum = 0 if field == "cpulimit" else 1
            if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
                raise TemplateUpdateInvalid(f"{field} must be an integer of at least {minimum}.")
        elif field == "image_aliases":
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise TemplateUpdateInvalid("image_aliases must be a list of strings.")
            value = [item.strip() for item in value if item.strip()]
        elif field not in {"second_console", "interface_naming"}:
            if not isinstance(value, str) or (field != "description" and not value.strip()):
                raise TemplateUpdateInvalid(f"{field} must be {'a' if field == 'description' else 'a non-empty'} string.")
            value = value.strip()
            if field == "console_type":
                try:
                    validate_console_mode_for_type(template.type, value)
                except ValueError as exc:
                    raise TemplateUpdateInvalid(str(exc)) from exc
            if field == "icon_type" and value not in ICON_TYPE_TO_FILENAME:
                raise TemplateUpdateInvalid("icon_type must be an available icon category.")
        payload[field] = value
    return payload


def _qemu_arg_machines(options: Any) -> set[str]:
    if not isinstance(options, str):
        return set()
    try:
        tokens = shlex.split(options)
    except ValueError:
        # Existing custom templates may have invalid arguments. Changed
        # arguments have already been checked by the engine value validator.
        return set()
    machines = set()
    for flag, argument in zip(tokens, tokens[1:]):
        if flag not in {"-machine", "-M"} or argument.startswith("-"):
            continue
        head, *modifiers = argument.split(",")
        machine = next((part[5:] for part in (head, *modifiers) if part.startswith("type=")), head)
        if "=" in machine:
            continue
        if machine == "q35" or machine.startswith(("pc-q35-", "q35-")):
            machines.add("q35")
        elif machine == "pc" or machine.startswith("pc-"):
            machines.add("pc")
        elif machine:
            machines.add(machine)
    return machines


def _validate_qemu_machine_edit(before: TemplateDefinition, after: TemplateDefinition) -> None:
    prior_machine = before.capabilities.get("machine")
    machine = after.capabilities.get("machine")
    prior_args = _qemu_arg_machines(before.extras.get("qemu_options"))
    args = _qemu_arg_machines(after.extras.get("qemu_options"))
    enabling_hotplug = after.capabilities.get("hotplug") and not before.capabilities.get("hotplug")
    if (machine != prior_machine or args != prior_args or enabling_hotplug) and args - {machine}:
        raise TemplateUpdateInvalid(
            "The machine type in Extra QEMU args conflicts with the template machine. "
            "Update or remove the -machine argument in the same edit."
        )


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


def _image_dependency_locations(candidate: Path) -> Iterator[Path]:
    """Follow the image and every parent and link needed to reach it."""
    pending = [candidate.absolute()]
    seen: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        yield path.resolve()
        parent = path.parent
        if parent != path:
            pending.append(parent)
        if path.is_symlink():
            pending.append(parent.resolve() / path.readlink())


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

    async def verify_checksum(self, image_type: str, name: str) -> dict[str, Any]:
        """Force-recompute the sha256 of the image's primary file.

        Runs in the default executor — hashing multi-GB disks must not
        block the event loop. The result is persisted keyed by
        (path, size, mtime_ns) so later requests reuse it.
        """
        path = self._find_image_path(image_type, name)
        target = _primary_file(path)
        if target is None:
            raise ImageLibraryError(f"No file to hash under {path}.")
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
    def _filesystem_image_entries(self, image_type: str) -> Iterator[tuple[str, Path]]:
        type_root = self.images_dir / image_type
        if not type_root.is_dir():
            return
        for child in sorted(type_root.iterdir()):
            if child.is_dir():
                yield child.name, child
            elif child.is_file():
                yield child.stem, child

    def _find_image_path(self, image_type: str, name: str) -> Path:
        if image_type == "docker" and any(image["name"] == name for image in self._scan_docker_images()):
            raise ImageLibraryError("Docker image checksums are managed by the Docker daemon.")
        if image_type in FILESYSTEM_IMAGE_TYPES:
            matches = [path for key, path in self._filesystem_image_entries(image_type) if key == name]
            if len(matches) == 1:
                return matches[0]
            if matches:
                raise ImageNameConflict(
                    f"More than one {image_type} image has the name {name}. "
                    "Image names must be unique within a runtime to verify or delete an image."
                )
        raise ImageNotFound(f"Image {name} ({image_type}) not found.")

    def _scan_filesystem_images(self) -> list[dict[str, Any]]:
        cache = self._load_cache()
        images: list[dict[str, Any]] = []
        for image_type in FILESYSTEM_IMAGE_TYPES:
            for name, child in self._filesystem_image_entries(image_type):
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
                        "name": name,
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

    def _iter_lab_nodes(self) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        if not self.labs_dir.exists():
            return
        for lab_file in sorted(iter_lab_json_files(self.labs_dir)):
            try:
                data = json.loads(lab_file.read_text())
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            nodes = data.get("nodes")
            if not isinstance(nodes, dict):
                continue
            lab_path = f"/{lab_file.relative_to(self.labs_dir).as_posix()}"
            metadata = data.get("meta")
            lab_name = metadata.get("name") if isinstance(metadata, dict) else None
            for node in nodes.values():
                if not isinstance(node, dict):
                    continue
                yield node, {
                    "lab_path": lab_path,
                    "lab": lab_file.name,
                    "lab_name": str(lab_name or lab_file.stem),
                    "node": str(node.get("name") or node.get("id") or ""),
                    "template": str(node.get("template") or ""),
                }

    def _runtime_image_paths(self, image_type: str, image_name: str) -> tuple[Path, ...]:
        if image_type == "qemu":
            from app.services.node_runtime_service import iter_qemu_media_candidates

            # An existing overlay can still use a lower-priority image.
            return tuple(
                path
                for iso in (False, True)
                for path, _ in iter_qemu_media_candidates(self.images_dir, image_name, iso=iso)
                if path.is_file()
            )
        if image_type == "iol":
            from app.services.runtime.iol import IolError, IolLauncher

            try:
                path = IolLauncher.resolve_image({"image": image_name}, self.images_dir / "iol")
            except IolError:
                return ()
            return (path,) if path.is_file() else ()
        if image_type == "dynamips":
            from app.services.runtime.dynamips import DynamipsError, DynamipsLauncher

            try:
                return (DynamipsLauncher._resolve_image_path({"image": image_name}),)
            except DynamipsError:
                return ()
        return ()

    def _scan_references(self, images: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        """Map inventory entries to saved references by runtime name and actual storage."""
        by_name: dict[tuple[str, str], set[str]] = {}
        by_path: dict[Path, set[str]] = {}
        for image in images:
            image_type, name, path = image["type"], image["name"], image["path"]
            by_name.setdefault((image_type, name), set()).add(path)
            if image_type in FILESYSTEM_IMAGE_TYPES:
                by_path.setdefault(Path(path).resolve(), set()).add(path)

        references: dict[str, list[dict[str, Any]]] = {}
        matched: dict[tuple[str, str], set[str]] = {}
        image_types = (*FILESYSTEM_IMAGE_TYPES, "docker")
        for node, reference in self._iter_lab_nodes():
            image_name = str(node.get("image") or "").strip()
            if not image_name:
                continue
            image_type = str(node.get("type") or "").strip().lower()
            if image_type == "iou":
                image_type = "iol"
            identity = (image_type, image_name)
            if identity not in matched:
                # Legacy records with no known runtime retain name-based protection.
                matching_types = (image_type,) if image_type in image_types else image_types
                targets: set[str] = set()
                for matching_type in matching_types:
                    targets.update(by_name.get((matching_type, image_name), ()))
                if by_path:
                    for candidate in self._runtime_image_paths(image_type, image_name):
                        for location in _image_dependency_locations(candidate):
                            targets.update(by_path.get(location, ()))
                matched[identity] = targets
            for target in matched[identity]:
                references.setdefault(target, []).append(reference)
        return references

    @staticmethod
    def _template_matches_image(template: dict[str, Any], image: dict[str, Any]) -> bool:
        if template.get("type") != image["type"]:
            return False
        # Filesystem types use the same alias/containment rule as the
        # node-creation picker so the library agrees with what actually
        # appears in the Add-node modal.
        shim = SimpleNamespace(name=template.get("name", ""), image_aliases=list(template.get("image_aliases", [])))
        return _matches_template_image_name(shim, image["name"])

    def inventory(self) -> dict[str, Any]:
        images = self._scan_filesystem_images() + self._scan_docker_images()
        references = self._scan_references(images)

        template_service = TemplateService()
        templates: list[dict[str, Any]] = []
        for template in template_service._load_templates():
            entry = template.as_response()
            entry["source"] = template.source
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
            image["referencedBy"] = references.get(image["path"], [])
            image["templates"] = [
                {"type": t["type"], "key": t["id"], "name": t["name"]}
                for t in templates
                if t["type"] == image["type"]
                and any(m["name"] == image["name"] for m in t["images"])
            ]

        return {
            "templates": templates,
            "images": images,
        }

    # ------------------------------------------------------------------
    # Template management
    # ------------------------------------------------------------------
    def create_template(
        self, template_type: str, key: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        key = key.strip()
        if not _TEMPLATE_KEY.fullmatch(key):
            raise TemplateUpdateInvalid(
                "Template keys must start with a letter or number and use up to 120 letters, numbers, dots, hyphens or underscores."
            )
        template_service = TemplateService()
        try:
            template_type = template_service._normalize_type(template_type)
        except TemplateError as exc:
            raise TemplateUpdateInvalid(str(exc)) from exc
        if payload.get("kind") == "paired":
            raise TemplateUpdateInvalid("Upload a standalone QEMU, Docker, IOL or Dynamips template.")
        user_dir = template_service.user_templates_dir
        if user_dir is None:
            raise ImageLibraryError("USER_TEMPLATES_DIR is not configured.")
        target = user_dir / template_type / f"{key}.yml"
        payload = {**payload, "type": template_type}

        with _template_edit_lock:
            _validate_template_path(target, template_service)
            if target.exists() or any(
                template.key == key and template.type == template_type
                for template in template_service._load_templates()
            ):
                raise TemplateConflict(f"Template {key} ({template_type}) already exists.")
            validated = _validate_new_template(template_service, template_type, target, payload)
            try:
                _write_template_atomically(target, payload, create=True)
            except FileExistsError as exc:
                raise TemplateConflict(f"Template {key} ({template_type}) already exists.") from exc
            except (OSError, ValueError, yaml.YAMLError) as exc:
                raise ImageLibraryError("Failed to write the template.") from exc
            return {**validated.as_response(), "source": validated.source}

    def upload_template(self, filename: str, content: bytes) -> dict[str, Any]:
        path = Path(filename)
        if path.suffix.lower() not in {".yml", ".yaml"} or any(
            character in filename for character in ("/", "\\", "\0")
        ):
            raise TemplateUpdateInvalid("Choose a template file ending in .yml or .yaml.")
        payload = _read_template_yaml(content)
        template_type = payload.get("type") or payload.get("kind")
        if not isinstance(template_type, str) or not template_type.strip():
            raise TemplateUpdateInvalid("Template YAML must declare a template type.")
        return self.create_template(template_type, path.stem, payload)

    def update_template(
        self, template_type: str, key: str, updates: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate and save a user template or a shadow of a shipped template."""
        with _template_edit_lock:
            template_service = TemplateService()
            template = template_service.get_template(template_type, key)
            if template.paired_parent is not None or template.raw.get("kind") == "paired":
                raise TemplateUpdateInvalid(
                    "Paired-template children are managed through their parent template."
                )

            user_dir = template_service.user_templates_dir
            if user_dir is None:
                raise ImageLibraryError("USER_TEMPLATES_DIR is not configured.")
            payload = _patched_template_payload(template, updates)
            if payload == template.raw:
                return template.as_response()

            target = template.user_template_path
            inherited_aliases = template.inherited_image_aliases
            if target is None:
                target = user_dir / f"{template.key}.yml"
                if target.exists():
                    target = user_dir / template.type / target.name
                if target.exists():
                    raise TemplateUpdateInvalid("A user template already occupies the override path.")
                payload.setdefault("type", template.type)
                inherited_aliases = template.image_aliases
                if "image_aliases" not in updates:
                    payload.pop("image_aliases", None)
            _validate_template_path(target, template_service)

            try:
                validated = template_service._definition_from_payload(
                    payload, target, template.type, user=True, inherited_image_aliases=inherited_aliases
                )
            except TemplateError as exc:
                raise TemplateUpdateInvalid(f"Edit rejected: {exc}") from exc
            if template.type == "qemu":
                _validate_qemu_machine_edit(template, validated)
            try:
                _write_template_atomically(target, payload)
            except (OSError, ValueError, yaml.YAMLError) as exc:
                raise ImageLibraryError(f"Failed to write template: {exc}") from exc
            return validated.as_response()

    def delete_template(self, template_type: str, key: str) -> dict[str, Any]:
        with _template_edit_lock:
            template_service = TemplateService()
            template = template_service.get_template(template_type, key)
            if template.paired_parent is not None or template.raw.get("kind") == "paired":
                raise TemplateConflict("Paired-template children are managed through their parent template.")
            target = template.user_template_path
            if target is None:
                raise TemplateConflict("Shipped templates cannot be deleted. You can remove a local override.")
            _validate_template_path(target, template_service)
            references = [
                reference
                for node, reference in self._iter_lab_nodes()
                if reference["template"] == key
                and str(node.get("type") or template.type).strip().lower() == template.type
            ]
            if references:
                raise TemplateReferenced(key, references)
            try:
                target.unlink()
            except OSError as exc:
                raise ImageLibraryError("Failed to remove the template.") from exc
            return {
                "type": template.type,
                "key": key,
                "name": template.name,
                "reverted": template.source == "override",
            }

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

        target = self._find_image_path(image_type, name)
        resolved = target.resolve()
        images_root = self.images_dir.resolve()
        if images_root not in resolved.parents:
            raise ImageLibraryError("Refusing to delete outside the images directory.")

        entry = {"type": image_type, "name": name, "path": str(target)}
        references = self._scan_references([entry]).get(str(target), [])
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
