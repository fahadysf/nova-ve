# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Image & template library API (#264).

Admin-gated endpoints behind the Library nav entry:

* ``GET  /api/library/templates``        — catalog with per-template on-box
  image matches (missing-image state is explicit).
* ``GET  /api/library/images``           — full inventory with size, cached
  sha256, mtime, referencing labs, and the templates resolving to each image.
* ``POST /api/library/images/{t}`` — upload a disk image for a filesystem runtime.
* ``POST /api/library/images/{t}/{n}/verify`` — force sha256 recompute.
* ``DELETE /api/library/images/{t}/{n}``  — guarded by lab references.
* ``PATCH /api/library/templates/{t}/{k}`` — persist editable fields (edits
  to builtin templates are shadowed under USER_TEMPLATES_DIR).
* ``POST /api/library/templates`` and ``POST /api/library/templates/upload``
  — create a user template from a form or a YAML file.
* ``DELETE /api/library/templates/{t}/{k}`` — remove an unused user template.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, File, Request, UploadFile
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile as MultipartUploadFile
from typing import Any, Literal

from app.dependencies import get_current_admin
from app.openapi import COMMON_RESPONSES
from app.schemas.user import UserRead
from app.services import audit_service
from app.services.image_library_service import (
    FILESYSTEM_IMAGE_TYPES,
    MAX_TEMPLATE_YAML_BYTES,
    ImageLibraryError,
    ImageLibraryService,
    ImageNameConflict,
    ImageNotFound,
    ImageReferenced,
    TemplateConflict,
    TemplateReferenced,
    TemplateUpdateInvalid,
    editable_template_extras_schema,
)
from app.services.template_service import ImageUploadConflict, TemplateError, TemplateService

router = APIRouter(
    prefix="/api/library",
    tags=["library"],
    responses=COMMON_RESPONSES,
)


def _ok(message: str, data) -> dict:
    return {"code": 200, "status": "success", "message": message, "data": data}


def _fail(message: str, *, code: int = 400, data=None) -> dict:
    return {"code": code, "status": "fail", "message": message, "data": data}


def _template_fields(template_type: str, template_key: str, fields) -> dict:
    """Current values of a template's editable ``fields`` (#300).

    Read to give the audit trail a before → after. Best-effort: it runs
    around an edit that must succeed or fail on its own terms, never on
    whether the trail could be written — an empty dict means "unknown".
    """
    try:
        template = TemplateService().get_template(template_type, template_key)
    except Exception:  # noqa: BLE001 — the audit entry must not fail the edit
        return {}
    return {field: getattr(template, field) for field in fields if hasattr(template, field)}


class TemplateEditBody(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    description: str | None = None
    icon_type: str | None = None
    cpu: int | None = Field(default=None, ge=1)
    ram: int | None = Field(default=None, ge=1)
    ethernet: int | None = Field(default=None, ge=1)
    console_type: str | None = None
    # Explicit null clears the secondary console; the edit service re-runs
    # the primary/secondary combination check on save.
    second_console: Literal["telnet", "vnc", "rdp"] | None = None
    cpulimit: int | None = Field(default=None, ge=0)
    image_aliases: list[str] | None = None
    interface_naming: dict[str, Any] | None = None
    capabilities: dict[str, Any] | None = None
    extras: dict[str, Any] | None = None


class TemplateCreateBody(TemplateEditBody):
    key: str
    type: str
    name: str = Field(min_length=1)


@router.get("/templates")
async def list_library_templates(
    current_user: UserRead = Depends(get_current_admin),
):
    service = ImageLibraryService()
    inventory = service.inventory()
    templates = []
    for template in inventory["templates"]:
        extras = template["extras"]
        extras_schema = editable_template_extras_schema(template["type"], extras)
        templates.append(
            {
                "type": template["type"],
                "key": template["id"],
                "source": template["source"],
                "name": template["name"],
                "description": template["description"],
                "icon_type": template["icon_type"],
                "cpu": template["cpu"],
                "ram": template["ram"],
                "ethernet": template["ethernet"],
                "console_type": template["console_type"],
                "second_console": template.get("second_console"),
                "cpulimit": template["cpulimit"],
                "image_aliases": template["image_aliases"],
                "capabilities": template["capabilities"],
                "interface_naming": template["interface_naming"],
                "extras": {field["key"]: extras[field["key"]] for field in extras_schema if field["key"] in extras},
                "extras_schema": extras_schema,
                "paired_parent": template.get("paired_parent"),
                "images": template["images"],
            }
        )
    return _ok("Library templates listed.", {"templates": templates})


async def _template_created(template: dict[str, Any], actor: str, *, via: str) -> dict:
    await audit_service.record(
        "template.create",
        actor=actor,
        target_type="template",
        target_id=f"{template['type']}/{template['id']}",
        target_name=template["name"],
        detail={"template_type": template["type"], "via": via},
    )
    return _ok("Template created.", template)


@router.post("/templates")
async def create_library_template(
    body: TemplateCreateBody,
    current_user: UserRead = Depends(get_current_admin),
):
    service = ImageLibraryService()
    payload = body.model_dump(exclude_unset=True, exclude={"type", "key"})
    try:
        created = service.create_template(body.type, body.key, payload)
    except TemplateUpdateInvalid as exc:
        return _fail(str(exc), code=422)
    except TemplateConflict as exc:
        return _fail(str(exc), code=409)
    except ImageLibraryError as exc:
        return _fail(str(exc))
    return await _template_created(created, current_user.username, via="form")


@router.post("/templates/upload")
async def upload_library_template(
    file: UploadFile = File(...),
    current_user: UserRead = Depends(get_current_admin),
):
    service = ImageLibraryService()
    try:
        content = await file.read(MAX_TEMPLATE_YAML_BYTES + 1)
        created = service.upload_template(file.filename or "", content)
    except TemplateUpdateInvalid as exc:
        return _fail(str(exc), code=422)
    except TemplateConflict as exc:
        return _fail(str(exc), code=409)
    except ImageLibraryError as exc:
        return _fail(str(exc))
    return await _template_created(created, current_user.username, via="yaml")


@router.get("/images")
async def list_library_images(
    current_user: UserRead = Depends(get_current_admin),
):
    service = ImageLibraryService()
    inventory = service.inventory()
    images = []
    for image in inventory["images"]:
        images.append(
            {
                "name": image["name"],
                "filename": image["filename"],
                "type": image["type"],
                "path": image["path"],
                "files": image["files"],
                "size": image["size"],
                "mtime": image["mtime"],
                "mtime_iso": (
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(image["mtime"]))
                    if image["mtime"]
                    else None
                ),
                "sha256": image["sha256"],
                "state": image["state"],
                "referenced_by": image["referencedBy"],
                "templates": image["templates"],
            }
        )
    return _ok("Library images listed.", {"images": images})


@router.post(
    "/images/{image_type}",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["image"],
                        "properties": {
                            "image": {"type": "string", "format": "binary"},
                            "image_name": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
)
async def upload_library_disk_image(
    image_type: str,
    request: Request,
    current_user: UserRead = Depends(get_current_admin),
):
    if image_type not in FILESYSTEM_IMAGE_TYPES:
        return _fail("Choose QEMU, IOL, or Dynamips for a disk image upload.")
    try:
        # Parsing can spool a large upload to disk. Authenticate before doing it.
        async with request.form(max_files=1, max_fields=1) as form:
            image = form.get("image")
            image_name = form.get("image_name")
            if not isinstance(image, MultipartUploadFile) or image.size == 0:
                return _fail("Choose a non-empty disk image.")
            if image_name is not None and not isinstance(image_name, str):
                return _fail("Image name must be text.")
            data, size = await TemplateService().upload_disk_image(
                image_type, image.filename or "", image.file, image_name,
            )
    except ImageUploadConflict as exc:
        return _fail(str(exc), code=409)
    except TemplateError as exc:
        return _fail(str(exc))
    await audit_service.record(
        "image.upload",
        actor=current_user.username,
        target_type="image",
        target_id=f"{image_type}/{data['image']}",
        target_name=data["image"],
        detail={"image_type": image_type, "bytes": size},
    )
    return _ok("Disk image uploaded.", data)


@router.post("/images/{image_type}/{image_name:path}/verify")
async def verify_image_checksum(
    image_type: str,
    image_name: str,
    current_user: UserRead = Depends(get_current_admin),
):
    try:
        result = await ImageLibraryService().verify_checksum(image_type, image_name)
    except ImageNotFound as exc:
        return _fail(str(exc), code=404)
    except ImageNameConflict as exc:
        return _fail(str(exc), code=409)
    except ImageLibraryError as exc:
        return _fail(str(exc))
    return _ok("Checksum computed.", result)


@router.delete("/images/{image_type}/{image_name:path}")
async def delete_library_image(
    image_type: str,
    image_name: str,
    current_user: UserRead = Depends(get_current_admin),
):
    service = ImageLibraryService()
    try:
        result = service.delete_image(image_type, image_name)
    except ImageNotFound as exc:
        return _fail(str(exc), code=404)
    except ImageNameConflict as exc:
        return _fail(str(exc), code=409)
    except ImageReferenced as exc:
        return _fail(str(exc), code=409, data={"references": exc.references})
    except ImageLibraryError as exc:
        return _fail(str(exc))
    # #300: the file is already gone — describe it from what was deleted.
    await audit_service.record(
        "image.delete",
        actor=current_user.username,
        target_type="image",
        target_id=f"{image_type}/{image_name}",
        target_name=image_name,
        detail={"image_type": image_type},
    )
    return _ok("Image deleted.", result)


@router.patch("/templates/{template_type}/{template_key}")
async def edit_library_template(
    template_type: str,
    template_key: str,
    body: TemplateEditBody,
    current_user: UserRead = Depends(get_current_admin),
):
    service = ImageLibraryService()
    updates = body.model_dump(exclude_unset=True)
    try:
        # The name is read too, so the entry can name the template even
        # when the edit left it alone.
        fields = (*updates, "name")
        before = _template_fields(template_type, template_key, fields)
        updated = service.update_template(template_type, template_key, updates)
        after = _template_fields(template_type, template_key, fields)
    except TemplateUpdateInvalid as exc:
        return _fail(str(exc), code=422)
    except ImageLibraryError as exc:
        return _fail(str(exc), code=400)
    except TemplateError as exc:
        # update_template's get_template lookup raises TemplateError on a
        # missing template — surface as 404.
        return _fail(str(exc), code=404)
    # #300: report what the edit changed. When both reads succeeded and
    # nothing differs, the edit was a no-op and is not an event; when a
    # read did not succeed, name the fields that were submitted instead.
    compared = bool(before) and bool(after)
    changes = audit_service.field_changes(before, after, skip=("extras",)) if compared else {}
    # Engine settings can contain credentials in command arguments or env
    # values. Audit which settings changed without copying their values.
    engine_keys = sorted(
        key for key in (updates.get("extras") or {})
        if before.get("extras", {}).get(key) != after.get("extras", {}).get(key)
    ) if compared else []
    if changes or engine_keys or not compared:
        detail = {"template_type": template_type}
        if compared:
            detail["changes"] = changes
            if engine_keys:
                detail["changed_engine_options"] = engine_keys
        else:
            detail["submitted"] = sorted(updates)
        await audit_service.record(
            "template.update",
            actor=current_user.username,
            target_type="template",
            target_id=f"{template_type}/{template_key}",
            target_name=after.get("name") or updates.get("name") or template_key,
            detail=detail,
        )
    return _ok("Template updated.", updated)


@router.delete("/templates/{template_type}/{template_key}")
async def delete_library_template(
    template_type: str,
    template_key: str,
    current_user: UserRead = Depends(get_current_admin),
):
    service = ImageLibraryService()
    try:
        removed = service.delete_template(template_type, template_key)
    except TemplateReferenced as exc:
        return _fail(str(exc), code=409, data={"references": exc.references})
    except TemplateConflict as exc:
        return _fail(str(exc), code=409)
    except TemplateUpdateInvalid as exc:
        return _fail(str(exc), code=422)
    except ImageLibraryError as exc:
        return _fail(str(exc))
    except TemplateError as exc:
        return _fail(str(exc), code=404)
    await audit_service.record(
        "template.delete",
        actor=current_user.username,
        target_type="template",
        target_id=f"{removed['type']}/{removed['key']}",
        target_name=removed["name"],
        detail={"template_type": removed["type"], "reverted_override": removed["reverted"]},
    )
    return _ok("Template override removed." if removed["reverted"] else "Template deleted.", removed)
