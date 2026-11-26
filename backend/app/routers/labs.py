# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import shutil
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.config import get_settings
from app.dependencies import get_current_user
from app.routers.folders import _scoped_folder_path
from app.schemas.lab import LabMetaCreate, LabMetaUpdate, LabMoveRequest
from app.schemas.network import NetworkCreate, NetworkUpdate
from app.schemas.node import (
    NodeBatchCreate,
    NodeCreate,
    NodeFromPairedTemplate,
    NodeFromTemplate,
    NodeUpdate,
    validate_console_mode_for_type,
    validate_second_console_mode_for_type,
)
from app.schemas.user import UserRead
from app.services.async_utils import notify_after_mutation, run_mutation, wait_for_mutation
from app.services.guacamole_db_service import GuacamoleDatabaseError, GuacamoleDatabaseService
from app.services.console_recording import get_console_recorder, lab_recordings_dir, recording_lab_id
from app.services import audit_service
from app.services.html5_service import Html5SessionError, Html5SessionService
from app.services.lab_service import (
    HIGH_WATER_FIELDS,
    LEGACY_SCHEMA_ERROR,
    LabIdExhaustedError,
    LabMembershipPathError,
    LabService,
    _lab_file_path,
    _normalize_relative_lab_path,
    allocate_network_id,
    allocate_node_id,
    build_relative_lab_path,
    lab_membership_lock,
    locked_lab_async,
)
from app.services.network_service import NetworkServiceError, network_service
from app.services.cloud_inventory_service import SharedCloudReferenceError
from app.services.shared_network_service import patch_violation
from app.services.node_runtime_service import NodeRuntimeError, NodeRuntimeService
from app.services.template_service import (
    TemplateError,
    TemplateService,
    _custom_icon_filename,
    _icon_filename_for,
    normalize_paired_child_kind,
    render_interface_name,
)
from app.services.ws_hub import ws_hub

from app.openapi import COMMON_RESPONSES

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/labs", tags=["labs"], responses=COMMON_RESPONSES)


class LegacyLabSchemaError(ValueError):
    """Raised when a v1 lab.json is read; converted to HTTP 422."""

    def __init__(self, lab_path: str):
        super().__init__(f"{LEGACY_SCHEMA_ERROR} (lab_path={lab_path})")
        self.lab_path = lab_path


def _legacy_schema_response(error: LegacyLabSchemaError) -> dict:
    return {
        "code": 422,
        "status": "fail",
        "message": LEGACY_SCHEMA_ERROR,
        "lab_path": error.lab_path,
    }


def _json_error(status_code: int, content: dict) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=content)


def _id_exhausted_response(exc: LabIdExhaustedError) -> JSONResponse:
    """The lab has used every id of a kind the host layer can name (#332)."""
    return _json_error(exc.code, {"code": exc.code, "status": "fail", "message": exc.message})


NODE_FIELDS_EDITABLE_WHILE_RUNNING = {"name", "icon", "left", "top", "interface_naming_scheme"}
NODE_FIELDS_EDITABLE_WHILE_STOPPED = {"image", "cpu", "ram", "ethernet", "console", "second_console", "delay", "extras"}
NODE_FIELDS_MUTABLE = NODE_FIELDS_EDITABLE_WHILE_RUNNING | NODE_FIELDS_EDITABLE_WHILE_STOPPED | {"config"}
NODE_CREATE_FIELDS = [
    "template",
    "image",
    "name_prefix",
    "count",
    "icon",
    "cpu",
    "ram",
    "ethernet",
    "console",
    "delay",
    "extras",
]
NODE_EDIT_FIELDS = [
    "name",
    "icon",
    "image",
    "cpu",
    "ram",
    "ethernet",
    "console",
    "delay",
    "extras",
]


def _read_lab_data(lab_path: str) -> dict:
    try:
        return LabService.read_lab_json_static(lab_path)
    except ValueError as exc:
        message = str(exc)
        if message.startswith(LEGACY_SCHEMA_ERROR):
            raise LegacyLabSchemaError(lab_path) from exc
        raise


@asynccontextmanager
async def _locked_lab(lab_path: str):
    """Hold the lab's ``lab_lock`` and yield ``(lab, None)``, read inside it (#328).

    Yields ``(None, response)`` when the lab cannot be read. Every route that
    changes a lab file writes through this, so the change is applied to the
    file as it is now and no other writer lands between the read and the
    write. Keep awaits that need the lab's lock out of the body.
    """
    async with AsyncExitStack() as stack:
        lab = error = None
        try:
            lab = await stack.enter_async_context(
                locked_lab_async(lab_path, reader=_read_lab_data)
            )
        except LegacyLabSchemaError as exc:
            error = _legacy_schema_response(exc)
        except FileNotFoundError:
            error = {"code": 404, "status": "fail", "message": "Lab does not exist (60038)."}
        yield lab, error


def _lab_revision(data: dict) -> int:
    """Machine-owned optimistic-concurrency revision (#245)."""
    meta = data.get("meta")
    if not isinstance(meta, dict):
        return 0
    try:
        return int(meta.get("revision") or 0)
    except (TypeError, ValueError):
        return 0


def _if_match_mismatch(request: Request, data: dict) -> int | None:
    """Server revision when an If-Match precondition fails, else None.

    An absent header means no precondition — backward compatible with
    older clients. Direct unit-test callers may pass a stub request with
    an empty headers mapping.
    """
    expected = request.headers.get("if-match")
    if expected is None:
        return None
    expected = expected.strip().strip('"')
    current = _lab_revision(data)
    if expected != str(current):
        return current
    return None


def _user_root(current_user: UserRead) -> str:
    if getattr(current_user, "role", "admin") == "admin":
        return ""
    return getattr(current_user, "folder", "/").strip().replace("\\", "/").strip("/")


def _scoped_lab_path(current_user: UserRead, raw_path: str, treat_as_absolute: bool) -> str:
    normalized = raw_path.strip().replace("\\", "/").strip("/")
    # #327: the prefix checks below compare strings, so a ``..`` segment
    # could pass them and only fail later at the file layer. Refuse it here.
    if ".." in normalized.split("/"):
        raise PermissionError("Access denied.")
    # #285: /Shared is a real read-write space for every authenticated user,
    # resolved at the labs root — never prefixed into a scoped user's root.
    if normalized == "Shared" or normalized.startswith("Shared/"):
        return normalized
    root = _user_root(current_user)
    if not root:
        return normalized

    if not normalized:
        return root

    if normalized == root or normalized.startswith(f"{root}/"):
        return normalized

    if treat_as_absolute or raw_path.strip().startswith("/"):
        raise PermissionError("Access denied.")

    return f"{root}/{normalized}"


class LabAccessDenied(Exception):
    """The caller may not open the lab named by the route's ``lab_path``."""


def lab_access_denied_handler(_request: Request, _exc: LabAccessDenied) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={"code": 403, "status": "fail", "message": "Access denied."},
    )


async def require_lab_access(
    lab_path: str,
    current_user: UserRead = Depends(get_current_user),
) -> str:
    """Refuse a ``{lab_path}`` outside the caller's folder or ``/Shared`` (#327).

    Every lab-path route declares this as a dependency, so the refusal comes
    before the handler validates its body, reads the lab or touches the host.
    """
    try:
        return _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
    except PermissionError as exc:
        raise LabAccessDenied(str(exc)) from exc


LAB_ACCESS = Depends(require_lab_access)


def _can_open_lab(current_user: UserRead, lab_path: str) -> bool:
    try:
        _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
    except PermissionError:
        return False
    return True


def _recording_lab_file(scoped_path: str) -> Path:
    lab_file = _lab_file_path(scoped_path)
    expected = get_settings().LABS_DIR.resolve() / _normalize_relative_lab_path(scoped_path)
    if lab_file != expected:
        raise PermissionError("Access denied.")
    return lab_file


def _default_interfaces(
    node_type: str,
    ethernet_count: int,
    interface_naming_scheme: str | None = None,
    template_interface_naming: dict | None = None,
) -> list[dict]:
    """Generate default interface list for a node.

    Priority (highest first):
    1. ``interface_naming_scheme`` — node-level override (US-105)
    2. Template-level format string or explicit names
    3. Hard-coded fallback: ``Gi{port}`` for qemu, ``eth{n}`` for everything else
    """
    interfaces = []
    template_naming = template_interface_naming or {}
    explicit_names = template_naming.get("explicit")
    for index in range(ethernet_count):
        if interface_naming_scheme is not None:
            name = render_interface_name(interface_naming_scheme, index)
        elif "format" in template_naming:
            name = render_interface_name(template_naming["format"], index)
        elif (
            isinstance(explicit_names, list)
            and index < len(explicit_names)
            and isinstance(explicit_names[index], str)
        ):
            name = explicit_names[index]
        elif node_type == "qemu":
            name = f"Gi{index + 1}"
        else:
            name = f"eth{index}"
        interfaces.append({
            "index": index,
            "name": name,
            "planned_mac": None,
            "port_position": None,
            "network_id": 0,
        })
    return interfaces


def _resize_interfaces(existing: list[dict], node_type: str, ethernet_count: int) -> list[dict]:
    resized = _default_interfaces(node_type, ethernet_count)
    for index, interface in enumerate(existing[:ethernet_count]):
        resized[index]["name"] = interface.get("name", resized[index]["name"])
        resized[index]["network_id"] = interface.get("network_id", 0)
        if "planned_mac" in interface:
            resized[index]["planned_mac"] = interface.get("planned_mac")
        if "port_position" in interface:
            resized[index]["port_position"] = interface.get("port_position")
    return resized


def _first_mac_for_node(lab_id: str, node_id: int) -> str:
    """Default first MAC, distinct per (lab, node).

    #319: the lab hash keeps same-numbered nodes of different labs apart when
    they meet on a shared network. The last octet is left for the NIC index.
    """
    digest = hashlib.blake2b(str(lab_id).encode("utf-8"), digest_size=2).digest()
    return (
        f"50:{digest[0]:02x}:{digest[1]:02x}:"
        f"{node_id & 0xFF:02x}:{(node_id >> 8) & 0xFF:02x}:00"
    )


_MAC_RE = re.compile(r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}")


def _apply_first_mac_edit(before: dict, node: dict) -> None:
    """Make an edited first MAC take effect (#319).

    Boot reads the top-level ``firstmac`` stamped at creation; the dialog edits
    ``extras.firstmac``. Addresses nova-ve derived from the old value are
    cleared so they are derived again.
    """
    if node.get("type") != "qemu":
        return
    old_extras = before.get("extras") if isinstance(before.get("extras"), dict) else {}
    new_extras = node.get("extras") if isinstance(node.get("extras"), dict) else {}
    new_mac = new_extras.get("firstmac")
    if not new_mac or new_mac == old_extras.get("firstmac"):
        return
    old_mac = before.get("firstmac") or old_extras.get("firstmac")
    node["firstmac"] = str(new_mac).lower()
    for index, interface in enumerate(node.get("interfaces") or []):
        planned = interface.get("planned_mac") if isinstance(interface, dict) else None
        if not planned or not _MAC_RE.fullmatch(str(old_mac or "")):
            continue
        if str(planned).lower() == NodeRuntimeService._mac_for_index(str(old_mac), index).lower():
            interface["planned_mac"] = None


def _html5_launcher_url(lab_path: str, node_id: int) -> str:
    quoted_path = quote(lab_path.strip("/"), safe="/")
    return f"/api/labs/{quoted_path}/nodes/{node_id}/html5"


def _node_identity(node: dict) -> tuple:
    """Fields that tell a node from a later one that reused its id.

    Edits do not change them; ``uuid`` is random per QEMU node.
    """
    extras = node.get("extras") if isinstance(node.get("extras"), dict) else {}
    return (node.get("type"), node.get("template"), node.get("uuid") or extras.get("uuid"))


def _node_is_running(lab_data: dict, node_id: int) -> bool:
    lab_id = str(lab_data.get("id", "")).strip()
    if not lab_id:
        return False
    node = lab_data.get("nodes", {}).get(str(node_id))
    if not node:
        return False
    enriched = NodeRuntimeService().enrich_node(lab_id, node_id, node)
    return int(enriched.get("status", 0)) == 2


def _validate_node_update_request(node: dict, request: NodeUpdate, node_running: bool) -> str | None:
    payload = request.model_dump(exclude_unset=True)
    requested_fields = set(payload.keys())
    invalid_fields = requested_fields - NODE_FIELDS_MUTABLE
    if invalid_fields:
        invalid_list = ", ".join(sorted(invalid_fields))
        return f"Unsupported node field update: {invalid_list}."

    # Treat a field as actually being changed only when the incoming value
    # differs from the stored value. The edit modal always re-submits the full
    # field set (so unchanged fields ride along on every save), and we must
    # not refuse a rename of a running node just because the payload echoes
    # back the same cpu/ram/ethernet/image/...
    def _changed(field: str) -> bool:
        return payload.get(field) != node.get(field)

    if node_running:
        blocked_fields = sorted(
            field
            for field in requested_fields
            if field in NODE_FIELDS_EDITABLE_WHILE_STOPPED and _changed(field)
        )
        if blocked_fields:
            blocked_list = ", ".join(blocked_fields)
            return f"Stop the node before changing: {blocked_list}."

    if "console" in requested_fields and _changed("console"):
        try:
            validate_console_mode_for_type(
                str(node.get("type", "qemu")),
                str(payload.get("console", "")),
            )
        except ValueError as exc:
            return str(exc)

    # The secondary must stay valid against the primary the node will end up
    # with: the incoming value when both change together, the stored one when
    # only the secondary changes.
    if "second_console" in requested_fields and _changed("second_console"):
        effective_primary = str(
            payload.get("console") if "console" in requested_fields else node.get("console", "telnet")
        )
        try:
            validate_second_console_mode_for_type(
                str(node.get("type", "qemu")),
                effective_primary,
                payload.get("second_console"),
            )
        except ValueError as exc:
            return str(exc)

    if "image" in requested_fields and _changed("image"):
        try:
            TemplateService().validate_node_request(
                str(node.get("type", "qemu")),
                str(node.get("template", "")),
                str(request.image or ""),
            )
        except TemplateError as exc:
            return str(exc)

    return None


def _node_position(index: int, left: int, top: int, placement: str) -> tuple[int, int]:
    if placement == "row":
        return left + (index * 180), top

    columns = 4
    return left + ((index % columns) * 180), top + ((index // columns) * 140)


def _build_node_payload(
    *,
    lab_id: str = "",
    node_id: int,
    request: NodeCreate | NodeBatchCreate,
    template,
    name: str,
    left: int,
    top: int,
) -> dict:
    provided_fields = request.model_fields_set
    ethernet = request.ethernet if "ethernet" in provided_fields else template.ethernet
    template_extras = TemplateService().template_extras(request.type, request.template)
    request_extras = dict(getattr(request, "extras", None) or {})
    merged_extras = {**template_extras, **request_extras}
    icon = request.icon
    if icon is None:
        # An empty override follows the template's current custom artwork.
        icon = "" if getattr(template, "custom_icon", None) else _icon_filename_for(template.icon_type)

    if request.type == "qemu":
        if not merged_extras.get("uuid"):
            merged_extras["uuid"] = str(uuid.uuid4())
        if not merged_extras.get("firstmac"):
            merged_extras["firstmac"] = _first_mac_for_node(lab_id, node_id)

    return {
        "id": node_id,
        "name": name,
        "type": request.type,
        "template": request.template,
        "image": request.image,
        "console": request.console if "console" in provided_fields else template.console_type,
        "second_console": _second_console_mode_from_create_request(request, template),
        "status": 0,
        "delay": request.delay if "delay" in provided_fields else 0,
        "cpu": request.cpu if "cpu" in provided_fields else template.cpu,
        "ram": request.ram if "ram" in provided_fields else template.ram,
        "ethernet": ethernet,
        "cpulimit": template.cpulimit,
        "uuid": merged_extras.get("uuid") if request.type == "qemu" else None,
        "firstmac": merged_extras.get("firstmac") if request.type == "qemu" else None,
        "left": left,
        "top": top,
        "icon": icon,
        "width": "0",
        "config": False,
        "config_list": [],
        "sat": 0,
        "computed_sat": 0,
        "interfaces": _default_interfaces(
            request.type,
            ethernet,
            interface_naming_scheme=getattr(request, "interface_naming_scheme", None),
            template_interface_naming=dict(template.interface_naming or {}),
        ),
        "extras": merged_extras,
    }


def _console_mode_from_create_request(request: NodeCreate | NodeBatchCreate, template) -> str:
    if "console" in request.model_fields_set:
        return str(request.console)
    return str(template.console_type or "telnet")


def _second_console_mode_from_create_request(request: NodeCreate | NodeBatchCreate, template) -> str | None:
    if "second_console" in request.model_fields_set:
        return request.second_console
    return getattr(template, "second_console", None) or None


def _validate_create_console_request(request: NodeCreate | NodeBatchCreate, template) -> str | None:
    try:
        validate_console_mode_for_type(
            str(request.type),
            _console_mode_from_create_request(request, template),
        )
        validate_second_console_mode_for_type(
            str(request.type),
            _console_mode_from_create_request(request, template),
            _second_console_mode_from_create_request(request, template),
        )
    except ValueError as exc:
        return str(exc)
    return None


@router.post("/")
async def create_lab(
    request: LabMetaCreate,
    current_user: UserRead = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    lab_service = LabService(db)
    try:
        scoped_path = _scoped_lab_path(current_user, request.path or "", treat_as_absolute=False)
        filename = build_relative_lab_path(request.name, path=scoped_path, filename=request.filename)
        async with lab_membership_lock(filename):
            lab, cancelled = await wait_for_mutation(asyncio.create_task(lab_service.create_lab(
                owner=current_user.username,
                name=request.name,
                filename=filename,
                locked=True,
                author=request.author,
                description=request.description,
                body=request.body,
                version=request.version,
                scripttimeout=request.scripttimeout,
                countdown=request.countdown,
                linkwidth=request.linkwidth,
                grid=request.grid,
                lock=request.lock,
                sat=request.sat,
            )))
    except FileExistsError as e:
        return {
            "code": 400,
            "status": "fail",
            "message": str(e),
        }
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    await notify_after_mutation(
        audit_service.record(
            "lab.create",
            actor=current_user.username,
            target_type="lab",
            target_id=lab.path,
            target_name=lab.name,
            lab_path=lab.path,
            lab_name=lab.name,
        ),
        cancelled,
    )
    return {
        "code": 200,
        "status": "success",
        "message": "Lab has been created (60021).",
        "data": {
            "id": str(lab.id),
            "filename": lab.filename,
            "name": lab.name,
            "path": lab.path,
            "owner": lab.owner,
            "author": lab.author,
            "description": lab.description,
            "body": lab.body,
            "version": lab.version,
            "scripttimeout": lab.scripttimeout,
            "countdown": lab.countdown,
            "linkwidth": lab.linkwidth,
            "grid": lab.grid,
            "lock": lab.lock,
            "sat": request.sat,
        },
    }


@router.put("/{lab_path:path}/meta", dependencies=[LAB_ACCESS])
async def update_lab(
    lab_path: str,
    request: LabMetaUpdate,
    current_user: UserRead = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    lab_service = LabService(db)
    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }
    async with lab_membership_lock(scoped_path):
        lab = await lab_service.get_lab_by_filename(scoped_path)
        if not lab:
            return {"code": 404, "status": "fail", "message": "Lab does not exist (60038)."}
        if lab.owner != current_user.username and current_user.role != "admin":
            return {"code": 403, "status": "fail", "message": "Access denied."}

        update_fields = request.model_dump(exclude_unset=True)
        # #300: snapshot the fields about to change for the audit trail.
        before = {field: getattr(lab, field) for field in update_fields if hasattr(lab, field)}
        lab, cancelled = await wait_for_mutation(
            asyncio.create_task(lab_service.update_lab(lab, locked=True, **update_fields))
        )
    # A save that changed nothing is not an event worth a row. Description
    # and body are reported by length — that they changed is the fact.
    changes = audit_service.field_changes(
        before, {field: getattr(lab, field) for field in before}
    )
    if changes:
        await notify_after_mutation(
            audit_service.record(
                "lab.update",
                actor=current_user.username,
                target_type="lab",
                target_id=lab.path,
                target_name=lab.name,
                lab_path=lab.path,
                lab_name=lab.name,
                detail={"changes": audit_service.text_lengths(changes, ("description", "body"))},
            ),
            cancelled,
        )
    elif cancelled:
        raise asyncio.CancelledError
    return {
        "code": 200,
        "status": "success",
        "message": "Lab has been saved (60022).",
        "data": {
            "id": str(lab.id),
            "filename": lab.filename,
            "name": lab.name,
            "path": lab.path,
            "owner": lab.owner,
            "author": lab.author,
            "description": lab.description,
            "body": lab.body,
            "version": lab.version,
            "scripttimeout": lab.scripttimeout,
            "countdown": lab.countdown,
            "linkwidth": lab.linkwidth,
            "grid": lab.grid,
            "lock": lab.lock,
        },
    }


@router.get("/{lab_path:path}/topology", dependencies=[LAB_ACCESS])
async def get_topology(
    lab_path: str,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        data = _read_lab_data(_scoped_lab_path(current_user, lab_path, treat_as_absolute=True))
    except LegacyLabSchemaError as exc:
        return _legacy_schema_response(exc)
    except FileNotFoundError:
        return {
            "code": 404,
            "status": "fail",
            "message": "Lab does not exist (60038).",
        }
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    return {
        "code": 200,
        "status": "success",
        "message": "Topology loaded",
        "data": data.get("topology", []),
    }


@router.put("/{lab_path:path}/topology", dependencies=[LAB_ACCESS])
async def update_topology(
    lab_path: str,
    request: Request,
    payload: dict | list = Body(...),
    current_user: UserRead = Depends(get_current_user),
):
    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    # #297: before-values of the node/network fields this payload patches.
    # The canvas saves link styling through this endpoint on every edit, so
    # only a payload that really patches a field earns an audit entry.
    patched_nodes: dict[str, dict] = {}
    patched_networks: dict[str, dict] = {}

    # Issue #174 follow-up: serialize topology writes against link_service /
    # network_service via lab_lock. Without this, two concurrent writers can
    # interleave and lose mutations. Plus, the post-write reconcile below has
    # to see the same JSON state we just wrote — read inside the lock.
    async with _locked_lab(scoped_path) as (lab, error):
        if error is not None:
            return error
        data = lab.data

        # #245: same optimistic-concurrency guard as PUT /layout.
        mismatch = _if_match_mismatch(request, data)
        if mismatch is not None:
            return JSONResponse(
                status_code=412,
                content={
                    "code": 412,
                    "status": "fail",
                    "message": "Lab changed elsewhere; reload and re-apply (stale revision).",
                    "revision": mismatch,
                },
            )

        if isinstance(payload, list):
            data["topology"] = payload
        else:
            data["topology"] = payload.get("topology", data.get("topology", []))
            # Positions are owned exclusively by PUT /layout. Accepting
            # left/top here lets a stale full-node echo (a client PUTting
            # its whole localNodes after a racing reload) revert positions
            # the layout endpoint already persisted — the "canvas resets to
            # previous state" bug. Skip them for both nodes and networks.
            _POSITION_FIELDS = frozenset({"left", "top"})
            for node_id, node_patch in payload.get("nodes", {}).items():
                node = data.get("nodes", {}).get(str(node_id))
                if node and isinstance(node_patch, dict):
                    for field, value in node_patch.items():
                        if field in _POSITION_FIELDS:
                            continue
                        if value is not None:
                            patched_nodes.setdefault(str(node_id), {})[field] = node.get(field)
                            node[field] = value
            # Bridge-Cloud security boundary: PUT /topology accepts only
            # layout/display fields for network patches.  Semantic fields
            # (``type``, ``runtime``, ``config``, ``id``, ``implicit``)
            # MUST go through ``NetworkService`` so the validated
            # create/update path stays the only writer of ownership
            # metadata.  Without this, an authenticated lab editor could
            # forge ``runtime.driver=bridge_cloud`` +
            # ``runtime.bridge_name=br-eth0`` and trick ``link_master_any``
            # into routing a TAP onto the physical host bridge.
            _NETWORK_TOPOLOGY_FIELDS = frozenset({
                "name", "icon", "width",
                "style", "linkstyle", "color", "label",
                "visibility", "smart",
            })
            for network_id, network_patch in payload.get("networks", {}).items():
                network = data.get("networks", {}).get(str(network_id))
                if network and isinstance(network_patch, dict):
                    for field, value in network_patch.items():
                        if field not in _NETWORK_TOPOLOGY_FIELDS:
                            continue
                        if value is not None:
                            patched_networks.setdefault(str(network_id), {})[field] = network.get(field)
                            network[field] = value

        lab.save()

        # Issue #174 follow-up: bridge the legacy PUT /topology bypass that
        # previously skipped runtime reconciliation. The UI deletes a link by
        # PUT /topology with the shorter array (NOT DELETE /links/{id}), and
        # write_lab_json_static regenerates links[] from topology[] — so links
        # disappear from lab.json without link_service.delete_link ever
        # firing. Result: kernel TAPs stayed on the bridge and QMP set_link
        # stayed UP, leaving guest carrier up forever even though the link
        # was "deleted" in lab.json. Mirror the reconcile-after-write block
        # from link_service.create_link / delete_link so any path that
        # mutates lab.json drives runtime + kernel state to match.
        try:
            data_after = LabService.read_lab_json_static(scoped_path)
            lab_id_after = str(data_after.get("id") or scoped_path)
            rt_svc = NodeRuntimeService()
            for raw_id, node_after in (data_after.get("nodes") or {}).items():
                if not isinstance(node_after, dict):
                    continue
                if str(node_after.get("type") or "").lower() != "qemu":
                    continue
                rt_svc.reconcile_qemu_node_links(
                    lab_id_after, data_after, node_after
                )
        except Exception:  # noqa: BLE001 — best-effort, observability only
            _logger.exception(
                "PUT /topology: post-write reconcile failed (lab=%s)",
                scoped_path,
            )

    await ws_hub.publish(lab.path, "topology_updated", {"revision": lab.revision})

    # #297: report the field patches the payload applied (see patched_nodes).
    nodes_after = data.get("nodes", {})
    networks_after = data.get("networks", {})
    topology_events: list[audit_service.AuditEvent] = []
    for node_id, before in patched_nodes.items():
        changes = audit_service.field_changes(before, nodes_after.get(node_id) or {})
        if changes:
            topology_events.append(
                audit_service.AuditEvent(
                    "node.update",
                    actor=current_user.username,
                    **audit_service.node_context(
                        data, node_id, scoped_path, detail={"changes": changes, "via": "topology"}
                    ),
                )
            )
    for network_id, before in patched_networks.items():
        changes = audit_service.field_changes(before, networks_after.get(network_id) or {})
        if changes:
            topology_events.append(
                audit_service.AuditEvent(
                    "network.update",
                    actor=current_user.username,
                    **audit_service.network_context(
                        data,
                        network_id,
                        scoped_path,
                        detail={"changes": changes, "via": "topology"},
                    ),
                )
            )
    await audit_service.record_batch(topology_events)
    return {
        "code": 200,
        "status": "success",
        "message": "Topology saved successfully.",
        # #245: post-write revision so the client can update its cached
        # token without a re-GET.
        "revision": lab.revision,
        "data": data.get("topology", []),
    }


@router.get("/{lab_path:path}/nodes", dependencies=[LAB_ACCESS])
async def list_nodes(
    lab_path: str,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
        data = _read_lab_data(scoped_path)
    except LegacyLabSchemaError as exc:
        return _legacy_schema_response(exc)
    except FileNotFoundError:
        return {
            "code": 404,
            "status": "fail",
            "message": "Lab does not exist (60038).",
        }
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    nodes = NodeRuntimeService().enrich_nodes(data)
    for key, node in nodes.items():
        node["url"] = _html5_launcher_url(scoped_path, int(key))

    return {
        "code": 200,
        "status": "success",
        "message": "Successfully listed nodes (60026).",
        "data": nodes,
    }


@router.get("/{lab_path:path}/node-catalog", dependencies=[LAB_ACCESS])
async def node_catalog(
    lab_path: str,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        _read_lab_data(_scoped_lab_path(current_user, lab_path, treat_as_absolute=True))
    except LegacyLabSchemaError as exc:
        return _legacy_schema_response(exc)
    except FileNotFoundError:
        return {
            "code": 404,
            "status": "fail",
            "message": "Lab does not exist (60038).",
        }
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    catalog = TemplateService().build_node_catalog()
    catalog["create_fields"] = NODE_CREATE_FIELDS
    catalog["edit_fields"] = NODE_EDIT_FIELDS
    catalog["runtime_editability"] = {
        "always": sorted(NODE_FIELDS_EDITABLE_WHILE_RUNNING),
        "stopped_only": sorted(NODE_FIELDS_EDITABLE_WHILE_STOPPED),
        "immutable": ["type", "template", "uuid"],
    }
    return {
        "code": 200,
        "status": "success",
        "message": "Node catalog loaded successfully.",
        "data": catalog,
    }


@router.post("/{lab_path:path}/nodes", dependencies=[LAB_ACCESS])
async def create_node(
    lab_path: str,
    request: NodeCreate,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
        data = _read_lab_data(scoped_path)
    except LegacyLabSchemaError as exc:
        return _legacy_schema_response(exc)
    except FileNotFoundError:
        return {
            "code": 404,
            "status": "fail",
            "message": "Lab does not exist (60038).",
        }
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    try:
        template = TemplateService().validate_node_request(
            request.type,
            request.template,
            request.image,
        )
    except TemplateError as exc:
        return {
            "code": 400,
            "status": "fail",
            "message": str(exc),
        }
    console_error = _validate_create_console_request(request, template)
    if console_error:
        return {
            "code": 400,
            "status": "fail",
            "message": console_error,
        }

    # #328: the id and the write come from the file as it is under the lock.
    async with _locked_lab(scoped_path) as (lab, error):
        if error is not None:
            return error
        data = lab.data
        nodes = data.setdefault("nodes", {})
        try:
            next_id = allocate_node_id(data)
        except LabIdExhaustedError as exc:
            # Unsaved, so the lab file is exactly as it was.
            return _id_exhausted_response(exc)
        node = _build_node_payload(
            lab_id=str(data.get("id") or scoped_path),
            node_id=next_id,
            request=request,
            template=template,
            name=request.name,
            left=request.left,
            top=request.top,
        )
        nodes[str(next_id)] = node
        lab.save()

    await ws_hub.publish(lab.path, "node_created", {"node_id": next_id, "revision": lab.revision})

    # #297: covers from-template creation too — that handler delegates here.
    await audit_service.record(
        "node.create",
        actor=current_user.username,
        **audit_service.node_context(
            data, next_id, scoped_path, detail={"image": node.get("image")}
        ),
    )
    return {
        "code": 200,
        "status": "success",
        "message": "Node created successfully.",
        "data": node,
        # #248: post-write revision for the editor's next If-Match write.
        "revision": lab.revision,
    }


@router.post("/{lab_path:path}/nodes/from-template", dependencies=[LAB_ACCESS])
async def create_node_from_template(
    lab_path: str,
    request: NodeFromTemplate,
    current_user: UserRead = Depends(get_current_user),
):
    """Instantiate a node from a template (#185).

    Looks up ``(template_type, template_key)`` in :class:`TemplateService` and
    creates a node with the template's defaults pre-filled, allowing the
    caller to override ``name`` / ``image`` / ``cpu`` / ``ram`` / ``ethernet``
    / ``console`` / ``left`` / ``top`` / ``extras``.

    Paired-node templates (``kind == "paired"`` from #188) are rejected with
    HTTP 400 until the multi-node picker UI follow-up lands (R-OOB-3).
    """
    template_service = TemplateService()

    if template_service.is_paired_user_template(request.template_key):
        return {
            "code": 400,
            "status": "fail",
            "message": (
                f"Template {request.template_key!r} is a paired-node template; "
                "use POST /api/labs/{lab_path}/nodes/from-paired-template instead "
                "(see #202)."
            ),
        }

    try:
        template_def = template_service.get_template(request.template_type, request.template_key)
    except TemplateError as exc:
        return {
            "code": 404,
            "status": "fail",
            "message": str(exc),
        }

    # #206 codex-iter2: synthetic per-child template entries (paired_parent set,
    # e.g. ``juniper-vmx__vcp``) must NOT be instantiable via the single-node
    # route. They live in the catalog only so node.template lookups in edit
    # mode + capability gates resolve; their console_type can violate
    # ``NodeCreate.console`` Literal (Juniper children declare ``serial``),
    # raising an uncaught Pydantic ValidationError. Reject explicitly.
    if template_def.paired_parent is not None:
        return {
            "code": 400,
            "status": "fail",
            "message": (
                f"Template {request.template_key!r} is a synthetic per-child entry "
                f"of paired template {template_def.paired_parent!r}; "
                "use POST /api/labs/{lab_path}/nodes/from-paired-template with "
                f"template_key={template_def.paired_parent!r} instead (see #206)."
            ),
        }

    # Build a NodeCreate from the template defaults, allowing per-request overrides.
    console_mode = request.console or template_def.console_type or "telnet"
    second_console_mode = (
        request.second_console
        if "second_console" in request.model_fields_set
        else (template_def.second_console or None)
    )
    try:
        validate_console_mode_for_type(request.template_type, console_mode)
        validate_second_console_mode_for_type(request.template_type, console_mode, second_console_mode)
    except ValueError as exc:
        return {
            "code": 400,
            "status": "fail",
            "message": str(exc),
        }

    node_create = NodeCreate(
        name=request.name,
        type=request.template_type,
        template=request.template_key,
        image=request.image,
        console=console_mode,
        second_console=second_console_mode,
        cpu=request.cpu if request.cpu is not None else template_def.cpu,
        ram=request.ram if request.ram is not None else template_def.ram,
        ethernet=request.ethernet if request.ethernet is not None else template_def.ethernet,
        left=request.left,
        top=request.top,
        extras=dict(request.extras),
    )
    return await create_node(lab_path, node_create, current_user=current_user)


class _PairedIfaceLookupError(ValueError):
    """Raised when a paired-link interface name doesn't match any child interface.

    Caught by ``create_nodes_from_paired_template`` to trigger lab.json rollback;
    paired templates are required to declare ``interface_naming.explicit`` per
    child so vendor-specific link references (Junos ``fxp0``/``em0``, ...) resolve
    exactly. The error message names the missing iface so the operator can fix
    the template definition.
    """


def _resolve_iface_index(node: dict, iface_name: str) -> int:
    """Find the interface index by name on a paired-template child node (#202).

    Strict: raises :class:`_PairedIfaceLookupError` when no interface matches.
    """
    interfaces = node.get("interfaces") or []
    for idx, iface in enumerate(interfaces):
        if isinstance(iface, dict) and str(iface.get("name") or "") == iface_name:
            return idx
    available = ", ".join(
        str(iface.get("name") or "") for iface in interfaces if isinstance(iface, dict)
    )
    raise _PairedIfaceLookupError(
        f"Paired-link references unknown interface {iface_name!r} on node "
        f"{node.get('name')!r} (available: {available or '<none>'}). "
        "Add interface_naming.explicit:[...] to the paired-template child so "
        "link references resolve exactly."
    )


def _build_paired_child_payload(
    *,
    lab_id: str = "",
    node_id: int,
    template_key: str,
    child: dict,
    child_id: str,
    name: str,
    left: int,
    top: int,
) -> dict:
    """Compose a lab-node payload from a paired-template child entry.

    Paired children are self-describing — image / cpu / ram / ethernet / console /
    extras come from the template JSON, not from a :class:`TemplateDefinition`
    lookup, so we build the payload directly instead of routing through
    :func:`_build_node_payload`. The child's ``interface_naming`` (if present)
    is honored so links that reference vendor-specific names (e.g. ``fxp0``)
    resolve cleanly via :func:`_resolve_iface_index`.

    #206: ``template`` is the synthetic per-child key
    ``<paired_key>__<child_id>`` so node.template lookups by edit-mode (image
    validation, capability gates, frontend modal resolution) succeed.
    """
    child_type = normalize_paired_child_kind(child.get("kind"))
    ethernet = int(child.get("ethernet", 1))
    extras = dict(child.get("extras") or {})
    icon_type = str(child.get("icon_type") or "router")
    icon = "" if _custom_icon_filename(child.get("icon")) else _icon_filename_for(icon_type)

    if child_type == "qemu":
        if not extras.get("uuid"):
            extras["uuid"] = str(uuid.uuid4())
        if not extras.get("firstmac"):
            extras["firstmac"] = _first_mac_for_node(lab_id, node_id)

    template_iface_naming = child.get("interface_naming")
    if not isinstance(template_iface_naming, dict):
        template_iface_naming = None

    interfaces = _default_interfaces(
        child_type,
        ethernet,
        interface_naming_scheme=None,
        template_interface_naming=template_iface_naming,
    )

    from app.services.template_service import synthetic_paired_child_key

    return {
        "id": node_id,
        "name": name,
        "type": child_type,
        "template": synthetic_paired_child_key(template_key, child_id),
        "image": str(child.get("image") or ""),
        "console": str(child.get("console") or "telnet"),
        "second_console": child.get("second_console") or None,
        "status": 0,
        "delay": 0,
        "cpu": int(child.get("cpu", 1)),
        "ram": int(child.get("ram", 1024)),
        "ethernet": ethernet,
        "cpulimit": int(child.get("cpulimit", 1)),
        "uuid": extras.get("uuid") if child_type == "qemu" else None,
        "firstmac": extras.get("firstmac") if child_type == "qemu" else None,
        "left": left,
        "top": top,
        "icon": icon,
        "width": "0",
        "config": False,
        "config_list": [],
        "sat": 0,
        "computed_sat": 0,
        "interfaces": interfaces,
        "extras": extras,
    }


@router.post("/{lab_path:path}/nodes/from-paired-template", dependencies=[LAB_ACCESS])
async def create_nodes_from_paired_template(
    lab_path: str,
    request: NodeFromPairedTemplate,
    current_user: UserRead = Depends(get_current_user),
):
    """Instantiate a paired-node template (#202): all children + auto-links, atomic.

    Paired templates (``kind="paired"``) declare two-or-more child nodes plus the
    intra-template links that connect them — vMX (VCP+VFP fxp0↔em0), vQFX
    (RE+PFE em1↔em1), and other multi-VM appliances. This endpoint creates ALL
    children and ALL auto-links in a single transaction; on partial failure
    lab.json is rolled back from a snapshot taken before the first mutation.
    """
    from app.services.template_service import validate_paired_template

    template_service = TemplateService()
    paired = template_service.get_paired_user_template(request.template_key)
    if paired is None:
        return _json_error(
            404,
            {
                "code": 404,
                "status": "fail",
                "message": (
                    f"Paired template {request.template_key!r} not found in "
                    "USER_TEMPLATES_DIR or is not a valid paired-node template."
                ),
            },
        )

    # #207 — pre-flight before any mutation. Catches old (#202-pre) imports
    # whose link refs reference interfaces no child will expose (e.g. paired
    # JSONs without ``interface_naming.explicit``). Surface as 422 so the UI
    # can render a fix-the-template message instead of a generic 500.
    pre_flight_reason = validate_paired_template(paired)
    if pre_flight_reason is not None:
        return _json_error(
            422,
            {
                "code": 422,
                "status": "fail",
                "message": (
                    f"Paired template {request.template_key!r} cannot be instantiated: "
                    f"{pre_flight_reason}"
                ),
            },
        )

    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
    except PermissionError as e:
        return _json_error(
            403,
            {
                "code": 403,
                "status": "fail",
                "message": str(e),
            },
        )

    from app.services.link_service import LinkService

    link_service = LinkService()
    created_nodes: list[dict] = []
    created_links: list[dict] = []

    # #208a — hold lab_lock across snapshot+phase1+phase2+restore so a
    # concurrent writer cannot slip in between snapshot and restore (which
    # would silently clobber their work). LinkService.create_link normally
    # acquires its own lab_lock; we pass _lab_lock_held=True so the inner
    # call uses a nullcontext (fcntl flock is not reentrant on Linux).
    async with _locked_lab(scoped_path) as (lab, error):
        if error is not None:
            return _json_error(error["code"], error)
        data = lab.data

        snapshot = copy.deepcopy(data)
        nodes_map = data.setdefault("nodes", {})

        child_id_to_node_id: dict[str, int] = {}

        try:
            for index, child in enumerate(paired["nodes"]):
                if not isinstance(child, dict):
                    raise ValueError(
                        f"Paired template {request.template_key!r} has malformed "
                        f"child entry at index {index}."
                    )
                child_id = str(child.get("id") or f"child-{index}")
                override_name = request.name_overrides.get(child_id)
                child_name = override_name or str(
                    child.get("name") or f"{request.template_key}-{child_id}"
                )
                left, top = _node_position(
                    index, request.base_left, request.base_top, "row"
                )
                next_id = allocate_node_id(data)
                payload = _build_paired_child_payload(
                    lab_id=str(data.get("id") or scoped_path),
                    node_id=next_id,
                    template_key=request.template_key,
                    child=child,
                    child_id=child_id,
                    name=child_name,
                    left=left,
                    top=top,
                )
                nodes_map[str(next_id)] = payload
                child_id_to_node_id[child_id] = next_id
                created_nodes.append(payload)

            lab.save()
        except LabIdExhaustedError as exc:
            # Nothing was saved, so there is nothing to roll back.
            return _id_exhausted_response(exc)
        except (ValueError, TemplateError) as exc:
            # #208-MEDIUM — bad child scalars (e.g. ``ethernet: "not-an-int"``)
            # and malformed-child entries are template defects, not server
            # bugs. Surface as 422 so the operator sees a fix-the-template
            # message instead of a generic 500. Pre-flight already catches
            # link/iface defects; this catches the scalar shape that pre-flight
            # doesn't (and shouldn't, to keep validator scope tight).
            lab.restore(snapshot)
            _logger.exception(
                "from-paired-template: node-creation phase failed for %s "
                "(template defect); rolled back",
                request.template_key,
            )
            return _json_error(
                422,
                {
                    "code": 422,
                    "status": "fail",
                    "message": (
                        f"Paired template {request.template_key!r} has malformed "
                        f"child data: {exc}"
                    ),
                },
            )
        except Exception as exc:  # noqa: BLE001 — broad catch is the rollback contract
            lab.restore(snapshot)
            _logger.exception(
                "from-paired-template: node-creation phase failed for %s; rolled back",
                request.template_key,
            )
            return _json_error(
                500,
                {
                    "code": 500,
                    "status": "fail",
                    "message": f"Failed to create paired nodes: {exc}",
                },
            )

        try:
            for link in paired["links"]:
                if not isinstance(link, dict):
                    continue
                from_child = str(link.get("from_node") or "")
                to_child = str(link.get("to_node") or "")
                from_iface_name = str(link.get("from_iface") or "")
                to_iface_name = str(link.get("to_iface") or "")

                from_node_id = child_id_to_node_id.get(from_child)
                to_node_id = child_id_to_node_id.get(to_child)
                if from_node_id is None or to_node_id is None:
                    raise ValueError(
                        f"Paired link references unknown child id "
                        f"({from_child!r}↔{to_child!r}) in template {request.template_key!r}."
                    )

                from_iface_idx = _resolve_iface_index(
                    nodes_map[str(from_node_id)], from_iface_name
                )
                to_iface_idx = _resolve_iface_index(
                    nodes_map[str(to_node_id)], to_iface_name
                )

                link_payload, _network_payload, _replayed = await link_service.create_link(
                    lab_path,
                    {"node_id": from_node_id, "interface_index": from_iface_idx},
                    {"node_id": to_node_id, "interface_index": to_iface_idx},
                    _lab_lock_held=True,
                )
                created_links.append(link_payload)
        except Exception as exc:  # noqa: BLE001 — broad catch is the rollback contract
            # #208b — compensate already-created links before snapshot restore
            # so host-side attach work, implicit bridges, and runtime stamps
            # are torn down (snapshot restore only reverts lab.json content;
            # kernel state survives without delete_link). Order: reverse of
            # creation. Errors during compensation are logged but do not
            # prevent snapshot restore — we want to leave the lab in the
            # cleanest state we can.
            for done in reversed(created_links):
                try:
                    await link_service.delete_link(
                        lab_path, str(done.get("id", "")), _lab_lock_held=True
                    )
                except Exception as comp_exc:  # noqa: BLE001 — best-effort cleanup
                    _logger.warning(
                        "from-paired-template: compensation delete_link(%s) "
                        "failed during rollback: %s",
                        done.get("id"),
                        comp_exc,
                    )
            lab.restore(snapshot)
            _logger.exception(
                "from-paired-template: link-creation phase failed for %s; rolled back",
                request.template_key,
            )
            # #208e — map exception classes to operator-meaningful HTTP codes.
            # _PairedIfaceLookupError = template defect → 422 (operator can
            # fix the template); LinkService DuplicateLinkError/LinkContention
            # = retryable / racy → 409; NodeRuntimeError / host_net.HostNetError
            # = hot-attach / kernel-side failure → 422 (mirrors the /links route
            # at routers/links.py:146,198,236, added in #208 codex-iter3 to
            # close the drift Codex flagged); everything else stays 500.
            from app.services import host_net
            from app.services.link_service import (
                DuplicateLinkError,
                InterfaceAlreadyAttachedError,
                LinkContentionError,
            )
            from app.services.node_runtime_service import NodeRuntimeError

            if isinstance(exc, _PairedIfaceLookupError):
                return _json_error(
                    422,
                    {
                        "code": 422,
                        "status": "fail",
                        "message": f"Paired link interface lookup failed: {exc}",
                    },
                )
            if isinstance(exc, DuplicateLinkError):
                return _json_error(
                    409,
                    {
                        "code": 409,
                        "status": "fail",
                        "message": f"Paired link conflicts with an existing link: {exc}",
                    },
                )
            if isinstance(exc, InterfaceAlreadyAttachedError):
                return _json_error(
                    409,
                    {
                        "code": 409,
                        "status": "fail",
                        "message": f"Paired link iface already wired: {exc}",
                    },
                )
            if isinstance(exc, LinkContentionError):
                return _json_error(
                    409,
                    {
                        "code": 409,
                        "status": "fail",
                        "message": f"Paired link blocked by concurrent attach/detach: {exc}",
                    },
                )
            # A paired link between two new nodes needs an implicit network.
            if isinstance(exc, LabIdExhaustedError):
                return _id_exhausted_response(exc)
            if isinstance(exc, (NodeRuntimeError, host_net.HostNetError)):
                return _json_error(
                    422,
                    {
                        "code": 422,
                        "status": "fail",
                        "message": f"Paired link hot-attach/host-net failure: {exc}",
                    },
                )
            return _json_error(
                500,
                {
                    "code": 500,
                    "status": "fail",
                    "message": f"Failed to create paired links: {exc}",
                },
            )
        # The links wrote the file after lab.save(); read what landed last.
        revision = LabService.read_meta_revision_static(lab.path)

    await ws_hub.publish(lab.path, "nodes_created", {"revision": revision})

    # #297: one entry per object the template brought into existence.
    await audit_service.record_batch(
        [
            audit_service.AuditEvent(
                "node.create",
                actor=current_user.username,
                **audit_service.node_context(
                    data,
                    payload.get("id"),
                    scoped_path,
                    detail={
                        "image": payload.get("image"),
                        "paired_template": request.template_key,
                    },
                ),
            )
            for payload in created_nodes
        ]
        + [
            audit_service.AuditEvent(
                "link.create",
                actor=current_user.username,
                **audit_service.link_context(
                    data,
                    payload,
                    scoped_path,
                    detail={"paired_template": request.template_key},
                ),
            )
            for payload in created_links
        ]
    )
    return {
        "code": 200,
        "status": "success",
        "message": (
            f"Paired template {request.template_key!r} instantiated: "
            f"{len(created_nodes)} node(s), {len(created_links)} link(s)."
        ),
        "data": {
            "nodes": created_nodes,
            "links": created_links,
        },
        # #248: post-write revision for the editor's next If-Match write.
        "revision": revision,
    }


@router.post("/{lab_path:path}/nodes/batch", dependencies=[LAB_ACCESS])
async def create_nodes_batch(
    lab_path: str,
    request: NodeBatchCreate,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
        data = _read_lab_data(scoped_path)
    except LegacyLabSchemaError as exc:
        return _legacy_schema_response(exc)
    except FileNotFoundError:
        return {
            "code": 404,
            "status": "fail",
            "message": "Lab does not exist (60038).",
        }
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    try:
        template = TemplateService().validate_node_request(
            request.type,
            request.template,
            request.image,
        )
    except TemplateError as exc:
        return {
            "code": 400,
            "status": "fail",
            "message": str(exc),
        }
    console_error = _validate_create_console_request(request, template)
    if console_error:
        return {
            "code": 400,
            "status": "fail",
            "message": console_error,
        }

    # #328: ids and the write come from the file as it is under the lock.
    async with _locked_lab(scoped_path) as (lab, error):
        if error is not None:
            return error
        data = lab.data
        nodes = data.setdefault("nodes", {})
        created_nodes = []
        for index in range(request.count):
            try:
                node_id = allocate_node_id(data)
            except LabIdExhaustedError as exc:
                # No partial batch: nothing is saved until every id is taken.
                return _id_exhausted_response(exc)
            left, top = _node_position(index, request.left, request.top, request.placement)
            node = _build_node_payload(
                lab_id=str(data.get("id") or scoped_path),
                node_id=node_id,
                request=request,
                template=template,
                name=f"{request.name_prefix}-{index + 1}",
                left=left,
                top=top,
            )
            nodes[str(node_id)] = node
            created_nodes.append(node)
        lab.save()

    await ws_hub.publish(lab.path, "nodes_created", {"revision": lab.revision})

    # #297: one entry per node — each is an object that now exists.
    await audit_service.record_batch(
        [
            audit_service.AuditEvent(
                "node.create",
                actor=current_user.username,
                **audit_service.node_context(
                    data,
                    node["id"],
                    scoped_path,
                    detail={"image": node.get("image"), "batch_size": request.count},
                ),
            )
            for node in created_nodes
        ]
    )
    return {
        "code": 200,
        "status": "success",
        "message": "Nodes created successfully.",
        "data": {
            "nodes": created_nodes,
        },
        # #248: post-write revision for the editor's next If-Match write.
        "revision": lab.revision,
    }


@router.get("/{lab_path:path}/nodes/{node_id}/interfaces", dependencies=[LAB_ACCESS])
async def list_interfaces(
    lab_path: str,
    node_id: int,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        data = _read_lab_data(_scoped_lab_path(current_user, lab_path, treat_as_absolute=True))
    except LegacyLabSchemaError as exc:
        return _legacy_schema_response(exc)
    except FileNotFoundError:
        return {
            "code": 404,
            "status": "fail",
            "message": "Lab does not exist (60038).",
        }
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    node = data.get("nodes", {}).get(str(node_id), {})
    return {
        "code": 200,
        "status": "success",
        "message": "Successfully listed node interfaces (60030).",
        "data": {
            "id": node_id,
            "sort": node.get("type", "qemu"),
            "ethernet": [
                {
                    "name": iface.get("name", f"eth{index}"),
                    "network_id": iface.get("network_id", 0),
                }
                for index, iface in enumerate(node.get("interfaces", []))
            ],
            "serial": [],
        },
    }


@router.get("/{lab_path:path}/nodes/{node_id}", dependencies=[LAB_ACCESS])
async def get_node(
    lab_path: str,
    node_id: int,
    current_user: UserRead = Depends(get_current_user),
):
    # Without this route, FastAPI falls back to the catch-all
    # ``GET /{lab_path:path}`` and tries to read
    # ``/var/lib/nova-ve/labs/<file>.json/nodes/<id>`` as a lab JSON,
    # which raises NotADirectoryError → 500. See issue #216.
    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
        data = _read_lab_data(scoped_path)
    except LegacyLabSchemaError as exc:
        return _legacy_schema_response(exc)
    except FileNotFoundError:
        return {
            "code": 404,
            "status": "fail",
            "message": "Lab does not exist (60038).",
        }
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    node = data.get("nodes", {}).get(str(node_id))
    if not node:
        return {
            "code": 404,
            "status": "fail",
            "message": "Node does not exist.",
        }

    lab_id = str(data.get("id", "")).strip()
    enriched = NodeRuntimeService().enrich_node(lab_id, node_id, node) if lab_id else node
    return {
        "code": 200,
        "status": "success",
        "message": "Node fetched successfully.",
        "data": enriched,
    }


@router.put("/{lab_path:path}/nodes/{node_id}", dependencies=[LAB_ACCESS])
async def update_node(
    lab_path: str,
    node_id: int,
    request: NodeUpdate,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    # #328: validate and apply the edit against the file as it is under the lock.
    async with _locked_lab(scoped_path) as (lab, error):
        if error is not None:
            return error
        data = lab.data
        node = data.get("nodes", {}).get(str(node_id))
        if not node:
            return {
                "code": 404,
                "status": "fail",
                "message": "Node does not exist.",
            }

        validation_error = _validate_node_update_request(
            node,
            request,
            _node_is_running(data, node_id),
        )
        if validation_error:
            return {
                "code": 400,
                "status": "fail",
                "message": validation_error,
            }

        new_mac = (request.extras or {}).get("firstmac")
        old_extras = node.get("extras") if isinstance(node.get("extras"), dict) else {}
        if (
            new_mac
            and new_mac != old_extras.get("firstmac")
            and not _MAC_RE.fullmatch(str(new_mac))
        ):
            return {
                "code": 400,
                "status": "fail",
                "message": "First MAC address must look like 50:00:00:01:00:00.",
            }

        # #297: shallow snapshot — the trail reports only what actually changed.
        before = dict(node)

        for field, value in request.model_dump(exclude_unset=True).items():
            # ``second_console: null`` is meaningful — it clears the secondary
            # console (#268). Every other field keeps the skip-None behavior.
            if value is not None or field == "second_console":
                node[field] = value

        node["interfaces"] = _resize_interfaces(
            node.get("interfaces", []),
            node.get("type", "qemu"),
            int(node.get("ethernet", 0)),
        )
        _apply_first_mac_edit(before, node)
        lab.save()

    await ws_hub.publish(lab.path, "node_updated", {"node_id": node_id, "revision": lab.revision})

    # A save that changed nothing (the edit dialog re-submits every field)
    # is not an event worth a row.
    changes = audit_service.field_changes(before, node, skip=("interfaces",))
    if changes:
        await audit_service.record(
            "node.update",
            actor=current_user.username,
            **audit_service.node_context(data, node_id, scoped_path, detail={"changes": changes}),
        )

    lab_id = str(data.get("id", "")).strip()
    enriched = NodeRuntimeService().enrich_node(lab_id, node_id, node) if lab_id else node

    return {
        "code": 200,
        "status": "success",
        "message": "Node updated successfully.",
        "data": enriched,
        # #248: post-write revision for the editor's next If-Match write.
        "revision": lab.revision,
    }


@router.delete("/{lab_path:path}/nodes/{node_id}", dependencies=[LAB_ACCESS])
async def delete_node(
    lab_path: str,
    node_id: int,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
        data = _read_lab_data(scoped_path)
    except LegacyLabSchemaError as exc:
        return _legacy_schema_response(exc)
    except FileNotFoundError:
        return {
            "code": 404,
            "status": "fail",
            "message": "Lab does not exist (60038).",
        }
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    nodes = data.get("nodes", {})
    if str(node_id) not in nodes:
        return {
            "code": 404,
            "status": "fail",
            "message": "Node does not exist.",
        }

    # #297: describe the node before it leaves the lab.
    audit_context = audit_service.node_context(data, node_id, scoped_path)

    # Host teardown runs before the lock: it can take a while and does not
    # write the lab file.
    try:
        NodeRuntimeService().wipe_node(data, node_id)
    except NodeRuntimeError:
        pass

    # #328: remove the node from the file as it is under the lock, so edits
    # made while the node was torn down are kept.
    async with _locked_lab(scoped_path) as (lab, error):
        if error is not None:
            return error
        data = lab.data
        # Deleted meanwhile, or the id now belongs to a node created since.
        current = data.get("nodes", {}).get(str(node_id))
        if not isinstance(current, dict) or _node_identity(current) != _node_identity(nodes[str(node_id)]):
            return {
                "code": 404,
                "status": "fail",
                "message": "Node does not exist.",
            }
        data["nodes"].pop(str(node_id))
        data["topology"] = [
            link
            for link in data.get("topology", [])
            if link.get("source") != f"node{node_id}" and link.get("destination") != f"node{node_id}"
        ]
        # Issue #174 follow-up: write_lab_json_static no longer regenerates
        # links[] from topology[] for v2 labs (it preserves links[] as
        # authoritative). Drop links[] entries referencing the deleted node
        # explicitly so the topology[] filter above doesn't leave stale links.
        data["links"] = [
            link
            for link in data.get("links", []) or []
            if not (
                isinstance(link, dict)
                and (
                    (isinstance(link.get("from"), dict) and link["from"].get("node_id") == node_id)
                    or (isinstance(link.get("to"), dict) and link["to"].get("node_id") == node_id)
                )
            )
        ]
        lab.save()

    await ws_hub.publish(lab.path, "node_deleted", {"node_id": node_id, "revision": lab.revision})
    await audit_service.record("node.delete", actor=current_user.username, **audit_context)
    return {
        "code": 200,
        "status": "success",
        "message": "Node deleted successfully.",
        # #248: post-write revision for the editor's next If-Match write.
        "revision": lab.revision,
    }


@router.get("/{lab_path:path}/nodes/{node_id}/start", dependencies=[LAB_ACCESS])
async def start_node(
    lab_path: str,
    node_id: int,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
        async with _locked_lab(scoped_path) as (lab, error):
            if error is not None:
                return error
            data = lab.data
            # Keep deletion out until the worker publishes its runtime record,
            # including when the client disconnects while starting the node.
            _, cancelled = await run_mutation(NodeRuntimeService().start_node, data, node_id)
    except LegacyLabSchemaError as exc:
        return _legacy_schema_response(exc)
    except FileNotFoundError as e:
        return {
            "code": 400,
            "status": "fail",
            "message": f"Runtime dependency or executable not found: {e}",
        }
    except NodeRuntimeError as e:
        return {
            "code": 400,
            "status": "fail",
            "message": str(e),
        }
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    await notify_after_mutation(
        audit_service.record(
            "node.start",
            actor=current_user.username,
            **audit_service.node_context(data, node_id, scoped_path),
        ),
        cancelled,
    )
    return {
        "code": 200,
        "status": "success",
        "message": "Node started (80049).",
    }


@router.get("/{lab_path:path}/nodes/{node_id}/stop", dependencies=[LAB_ACCESS])
async def stop_node(
    lab_path: str,
    node_id: int,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
        data = _read_lab_data(scoped_path)
        NodeRuntimeService().stop_node(data, node_id)
    except LegacyLabSchemaError as exc:
        return _legacy_schema_response(exc)
    except FileNotFoundError:
        return {
            "code": 404,
            "status": "fail",
            "message": "Lab does not exist (60038).",
        }
    except NodeRuntimeError as e:
        return {
            "code": 400,
            "status": "fail",
            "message": str(e),
        }
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    await audit_service.record(
        "node.stop",
        actor=current_user.username,
        **audit_service.node_context(data, node_id, scoped_path),
    )
    return {
        "code": 200,
        "status": "success",
        "message": "Node stopped (80050).",
    }


@router.get("/{lab_path:path}/nodes/{node_id}/wipe", dependencies=[LAB_ACCESS])
async def wipe_node(
    lab_path: str,
    node_id: int,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
        data = _read_lab_data(scoped_path)
        NodeRuntimeService().wipe_node(data, node_id)
    except LegacyLabSchemaError as exc:
        return _legacy_schema_response(exc)
    except FileNotFoundError:
        return {
            "code": 404,
            "status": "fail",
            "message": "Lab does not exist (60038).",
        }
    except NodeRuntimeError as e:
        return {
            "code": 400,
            "status": "fail",
            "message": str(e),
        }
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    await audit_service.record(
        "node.wipe",
        actor=current_user.username,
        **audit_service.node_context(data, node_id, scoped_path),
    )
    return {
        "code": 200,
        "status": "success",
        "message": "Node cleared (80053).",
    }


@router.get("/{lab_path:path}/nodes/{node_id}/logs", dependencies=[LAB_ACCESS])
async def node_logs(
    lab_path: str,
    node_id: int,
    tail: int = 200,
    follow: bool = False,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        data = _read_lab_data(_scoped_lab_path(current_user, lab_path, treat_as_absolute=True))
    except LegacyLabSchemaError as exc:
        return _legacy_schema_response(exc)
    except FileNotFoundError:
        return {
            "code": 404,
            "status": "fail",
            "message": "Lab does not exist (60038).",
        }
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    runtime_service = NodeRuntimeService()
    lab_id = str(data.get("id"))
    if follow:
        return StreamingResponse(
            runtime_service.stream_logs(lab_id, node_id, tail=tail),
            media_type="text/plain",
        )

    return {
        "code": 200,
        "status": "success",
        "message": "Node logs fetched.",
        "data": {"logs": runtime_service.read_logs(lab_id, node_id, tail=tail)},
    }


@router.get("/{lab_path:path}/nodes/{node_id}/console", dependencies=[LAB_ACCESS])
async def node_console(
    lab_path: str,
    node_id: int,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
        data = _read_lab_data(scoped_path)
        console = NodeRuntimeService().console_info(data, node_id)
    except LegacyLabSchemaError as exc:
        return _legacy_schema_response(exc)
    except FileNotFoundError:
        return {
            "code": 404,
            "status": "fail",
            "message": "Lab does not exist (60038).",
        }
    except NodeRuntimeError as exc:
        return {
            "code": 400,
            "status": "fail",
            "message": str(exc),
        }
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    console["url"] = _html5_launcher_url(scoped_path, node_id)

    return {
        "code": 200,
        "status": "success",
        "message": "Node console fetched.",
        "data": console,
    }


@router.get("/{lab_path:path}/nodes/{node_id}/qemu-preview", dependencies=[LAB_ACCESS])
async def node_qemu_preview(
    lab_path: str,
    node_id: int,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
        data = _read_lab_data(scoped_path)
        preview = NodeRuntimeService().qemu_command_preview(data, node_id)
    except LegacyLabSchemaError as exc:
        return _legacy_schema_response(exc)
    except FileNotFoundError:
        return {"code": 404, "status": "fail", "message": "Lab does not exist."}
    except NodeRuntimeError as exc:
        return {"code": 400, "status": "fail", "message": str(exc)}

    return {"code": 200, "status": "success", "message": "QEMU command preview.", "data": preview}


@router.get("/{lab_path:path}/nodes/{node_id}/telnet", dependencies=[LAB_ACCESS])
async def node_telnet(
    lab_path: str,
    node_id: int,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
        data = _read_lab_data(scoped_path)
        console = NodeRuntimeService().console_info(data, node_id)
    except LegacyLabSchemaError as exc:
        return _legacy_schema_response(exc)
    except FileNotFoundError:
        return {
            "code": 404,
            "status": "fail",
            "message": "Lab does not exist (60038).",
        }
    except NodeRuntimeError as exc:
        return {
            "code": 400,
            "status": "fail",
            "message": str(exc),
        }
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    if console["console"] != "telnet":
        return {
            "code": 400,
            "status": "fail",
            "message": "Node console is not telnet.",
        }

    await audit_service.record_console_open(
        actor=current_user.username,
        lab_data=data,
        node_id=node_id,
        lab_path=scoped_path,
        via="native",
        protocol="telnet",
    )
    body = f"telnet://{console['host']}:{console['port']}\n"
    response = PlainTextResponse(body, media_type="application/x-telnet")
    response.headers["Content-Disposition"] = f'attachment; filename="node-{node_id}.telnet"'
    return response


@router.get("/{lab_path:path}/nodes/{node_id}/rdp", dependencies=[LAB_ACCESS])
async def node_rdp(
    lab_path: str,
    node_id: int,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
        data = _read_lab_data(scoped_path)
        console = NodeRuntimeService().console_info(data, node_id)
    except LegacyLabSchemaError as exc:
        return _legacy_schema_response(exc)
    except FileNotFoundError:
        return {
            "code": 404,
            "status": "fail",
            "message": "Lab does not exist (60038).",
        }
    except NodeRuntimeError as exc:
        return {
            "code": 400,
            "status": "fail",
            "message": str(exc),
        }
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    if console["console"] != "rdp":
        return {
            "code": 400,
            "status": "fail",
            "message": "Node console is not RDP.",
        }

    await audit_service.record_console_open(
        actor=current_user.username,
        lab_data=data,
        node_id=node_id,
        lab_path=scoped_path,
        via="native",
        protocol="rdp",
    )
    body = "\n".join(
        [
            f"full address:s:{console['host']}:{console['port']}",
            "prompt for credentials:i:1",
            "administrative session:i:0",
        ]
    )
    response = PlainTextResponse(body, media_type="application/x-rdp")
    response.headers["Content-Disposition"] = f'attachment; filename="node-{node_id}.rdp"'
    return response


# External console launch (#269): the OS protocol handlers behind
# telnet://, vnc:// and rdp:// URLs. The endpoint advertises the
# admin-configured externally reachable host plus the node's console port so
# the operator's desktop client can dial the console directly. Credentials
# are never embedded — the URL surface is host:port only, and access is
# guarded exactly like the sibling console routes.
_EXTERNAL_CONSOLE_SCHEMES = {
    "telnet": "telnet",
    "vnc": "vnc",
    "rdp": "rdp",
}


@router.get("/{lab_path:path}/nodes/{node_id}/external-console", dependencies=[LAB_ACCESS])
async def node_external_console(
    lab_path: str,
    node_id: int,
    mode: str = "primary",
    current_user: UserRead = Depends(get_current_user),
):
    """Advertise an externally reachable console endpoint for a running node.

    Returns ``{mode, scheme, host, port, console}`` where ``scheme`` is one of
    ``telnet|vnc|rdp`` and ``host`` is the EXTERNAL_CONSOLE_HOST setting used
    verbatim. The client builds the OS scheme URL (``telnet://host:port`` and
    friends) itself. Docker-backed consoles are advertised through the console
    proxy listener that already republishes the container console port on the
    wildcard bind.
    """
    mode_normalized = (mode or "primary").strip().lower()
    if mode_normalized not in {"primary", "second"}:
        return {"code": 422, "status": "fail", "message": "mode must be 'primary' or 'second'."}

    external_host = str(getattr(get_settings(), "EXTERNAL_CONSOLE_HOST", "") or "").strip()
    if not external_host:
        return {
            "code": 400,
            "status": "fail",
            "message": (
                "External console launching is disabled: no externally reachable "
                "console host is configured."
            ),
        }

    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
        data = _read_lab_data(scoped_path)
        runtime_service = NodeRuntimeService()
        lab_id = str(data.get("id", "")).strip()
        # Registry-authoritative liveness (#262): a persisted status=2 on a
        # stopped node must not yield a dialable-looking endpoint.
        if not runtime_service.is_node_running(lab_id, node_id):
            return {
                "code": 400,
                "status": "fail",
                "message": f"Node is not running: {node_id}.",
            }

        node = data.get("nodes", {}).get(str(node_id), {})
        if mode_normalized == "second" and not node.get("second_console"):
            return {
                "code": 400,
                "status": "fail",
                "message": f"Node has no secondary console: {node_id}.",
            }

        # console_info re-spawns a dead docker console proxy before reporting
        # the port, so the advertised host:port actually accepts connections.
        # mode="second" (#268) resolves the secondary console session — its
        # port is wildcard-bound too while external consoles are enabled.
        console = runtime_service.console_info(data, node_id, mode=mode_normalized)
    except LegacyLabSchemaError as exc:
        return _legacy_schema_response(exc)
    except FileNotFoundError:
        return {
            "code": 404,
            "status": "fail",
            "message": "Lab does not exist (60038).",
        }
    except NodeRuntimeError as exc:
        return {
            "code": 400,
            "status": "fail",
            "message": str(exc),
        }
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    console_mode = str(console.get("console", "telnet"))
    scheme = _EXTERNAL_CONSOLE_SCHEMES.get(console_mode)
    if scheme is None:
        return {
            "code": 400,
            "status": "fail",
            "message": f"Console mode '{console_mode}' has no native client launch support.",
        }

    port = int(console.get("port", 0))
    if port <= 0:
        return {
            "code": 400,
            "status": "fail",
            "message": "Node console port is not available.",
        }

    # #300: the console is being handed to a client that dials the node
    # directly — this hand-off is the only moment the server sees it.
    await audit_service.record_console_open(
        actor=current_user.username,
        lab_data=data,
        node_id=node_id,
        lab_path=scoped_path,
        console=mode_normalized,
        via="external",
        protocol=console_mode,
    )
    return {
        "code": 200,
        "status": "success",
        "message": "External console endpoint fetched.",
        "data": {
            "mode": mode_normalized,
            "scheme": scheme,
            "host": external_host,
            "port": port,
            "console": console_mode,
        },
    }


@router.get("/{lab_path:path}/nodes/{node_id}/html5", dependencies=[LAB_ACCESS])
async def node_html5(
    lab_path: str,
    node_id: int,
    console: str = "primary",
    current_user: UserRead = Depends(get_current_user),
):
    # #268 — ``console`` selects which declared console session to open:
    # ``primary`` (default) or ``second`` when the node exposes one.
    if console not in ("primary", "second"):
        return {
            "code": 400,
            "status": "fail",
            "message": f"Unknown console mode {console!r}; expected 'primary' or 'second'.",
        }

    if not getattr(current_user, "html5", True):
        return {
            "code": 403,
            "status": "fail",
            "message": "HTML5 console access is disabled for this user.",
        }

    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
        data = _read_lab_data(scoped_path)
        runtime_service = NodeRuntimeService()
        console_info = runtime_service.console_info(data, node_id, mode=console)
    except LegacyLabSchemaError as exc:
        return _legacy_schema_response(exc)
    except FileNotFoundError:
        return {
            "code": 404,
            "status": "fail",
            "message": "Lab does not exist (60038).",
        }
    except NodeRuntimeError as exc:
        return {
            "code": 400,
            "status": "fail",
            "message": str(exc),
        }
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    try:
        settings = get_settings()
        console_host = console_info["host"]
        console_port = int(console_info["port"])
        if console_info["console"] == "telnet":
            try:
                lab_file = _recording_lab_file(scoped_path)
            except (PermissionError, ValueError):
                recording = None
            else:
                recording = await get_console_recorder().start_session(
                    lab_id=recording_lab_id(lab_file, data),
                    lab_file=lab_file,
                    lab_name=str((data.get("meta") or {}).get("name") or lab_path),
                    node_id=node_id,
                    node_name=(data.get("nodes", {}).get(str(node_id)) or {}).get("name") or console_info["name"],
                    target_host=console_host,
                    target_port=console_port,
                    username=current_user.username,
                    mode=console,
                )
            if recording is not None:
                console_port = recording.listen_port
        rdp_password = None
        if console_info["console"] == "rdp":
            rdp_password = await asyncio.to_thread(runtime_service.platform_rdp_password, data, node_id)
        if settings.GUACAMOLE_DATABASE_URL.strip():
            # The secondary session gets its own stable connection key so the
            # two consoles of one node never share a Guacamole connection.
            connection_key = f"{data.get('id', lab_path)}:{node_id}:{console_info['console']}"
            if console == "second":
                connection_key = f"{data.get('id', lab_path)}:{node_id}:second:{console_info['console']}"
            html5_url = await GuacamoleDatabaseService().create_console_url(
                current_user,
                host=console_host,
                port=console_port,
                protocol=console_info["console"],
                connection_name=console_info["name"],
                connection_key=connection_key,
                rdp_password=rdp_password,
            )
        else:
            html5_url = await Html5SessionService().create_console_url(
                current_user,
                host=console_host,
                port=console_port,
                protocol=console_info["console"],
                connection_name=console_info["name"],
                rdp_password=rdp_password,
            )
    except (Html5SessionError, GuacamoleDatabaseError, NodeRuntimeError) as exc:
        return {
            "code": 500,
            "status": "fail",
            "message": str(exc),
        }

    # #300: one request here is one console genuinely opened — the page
    # never restores tabs on load, and re-opening an open console only
    # focuses it.
    await audit_service.record_console_open(
        actor=current_user.username,
        lab_data=data,
        node_id=node_id,
        lab_path=scoped_path,
        console=console,
        via="html5",
        protocol=console_info["console"],
    )
    return RedirectResponse(url=html5_url, status_code=307)


@router.post("/{lab_path:path}/nodes/{node_id}/console/close", dependencies=[LAB_ACCESS])
async def node_console_close(
    lab_path: str,
    node_id: int,
    console: str = "primary",
    current_user: UserRead = Depends(get_current_user),
):
    """The lab page reports that an in-browser console was closed (#300).

    Closing a tab only unloads an iframe, so nothing else tells the server
    a console session ended. The report is sent with ``keepalive`` and can
    arrive while the page is being torn down, so it stays cheap: the scope
    check is enforced, and the lab is read only to name the node — a lab
    deleted while its console was open still gets its close recorded.
    """
    if console not in ("primary", "second"):
        return {
            "code": 400,
            "status": "fail",
            "message": f"Unknown console mode {console!r}; expected 'primary' or 'second'.",
        }
    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }
    try:
        data = _read_lab_data(scoped_path)
    except Exception:  # noqa: BLE001 — naming the node is best-effort
        data = None

    duration = await audit_service.record_console_close(
        actor=current_user.username,
        lab_data=data,
        node_id=node_id,
        lab_path=scoped_path,
        console=console,
    )
    return {
        "code": 200,
        "status": "success",
        "message": "Console close recorded.",
        "data": {"duration": duration},
    }


@router.get("/{lab_path:path}/networks", dependencies=[LAB_ACCESS])
async def list_networks(
    lab_path: str,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        data = _read_lab_data(_scoped_lab_path(current_user, lab_path, treat_as_absolute=True))
    except LegacyLabSchemaError as exc:
        return _legacy_schema_response(exc)
    except FileNotFoundError:
        return {
            "code": 404,
            "status": "fail",
            "message": "Lab does not exist (60038).",
        }
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    return {
        "code": 200,
        "status": "success",
        "message": "Successfully listed networks (60004).",
        "data": data.get("networks", {}),
    }


@router.post("/{lab_path:path}/networks", dependencies=[LAB_ACCESS])
async def create_network(
    lab_path: str,
    request: NetworkCreate,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    # #328: the id and the write come from the file as it is under the lock.
    async with _locked_lab(scoped_path) as (lab, error):
        if error is not None:
            return error
        data = lab.data
        networks = data.setdefault("networks", {})
        try:
            next_id = allocate_network_id(data)
        except LabIdExhaustedError as exc:
            # Unsaved, so the lab file is exactly as it was.
            return _id_exhausted_response(exc)
        network = {
            "id": next_id,
            "name": request.name,
            "type": request.type,
            "left": request.left,
            "top": request.top,
            "icon": "01-Cloud-Default.svg",
            "width": 0,
            "style": "Solid",
            "linkstyle": "Straight",
            "color": "",
            "label": "",
            "visibility": True,
            "implicit": False,
            "smart": -1,
            "config": {},
        }
        networks[str(next_id)] = network
        lab.save()
    network["count"] = 0

    return {
        "code": 200,
        "status": "success",
        "message": "Network created successfully.",
        "data": network,
        # #248: post-write revision for the editor's next If-Match write.
        "revision": lab.revision,
    }


@router.put("/{lab_path:path}/networks/{network_id}", dependencies=[LAB_ACCESS])
async def update_network(
    lab_path: str,
    network_id: int,
    request: NetworkUpdate,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    # #328: validate and apply against the file as it is under the lock. A
    # NAT-Cloud edit is handed to the network service after the lock is
    # released; it takes the lock itself and validates again.
    async with _locked_lab(scoped_path) as (lab, error):
        if error is not None:
            return error
        data = lab.data
        network = data.get("networks", {}).get(str(network_id))
        if not network:
            return {
                "code": 404,
                "status": "fail",
                "message": "Network does not exist.",
            }

        # Bridge-Cloud security: same rule as ``NetworkService.patch_network``
        # — refuse type↔bridge_cloud transitions and refuse setting
        # ``config.host_bridge`` on a non-bridge_cloud record.  See plan
        # §6.2 T2 + .omc/plans/bridge-cloud-feature.md.
        patch = request.model_dump(exclude_unset=True)
        current_type = str(network.get("type", "linux_bridge") or "linux_bridge")
        new_type = patch.get("type")
        if new_type is not None and new_type != current_type:
            if new_type == "bridge_cloud" or current_type == "bridge_cloud":
                return {
                    "code": 422,
                    "status": "fail",
                    "message": (
                        "Cannot mutate network type to/from 'bridge_cloud' via "
                        "update; delete and recreate the network instead."
                    ),
                }
        new_config = patch.get("config")
        if isinstance(new_config, dict) and "host_bridge" in new_config:
            if current_type != "bridge_cloud":
                return {
                    "code": 422,
                    "status": "fail",
                    "message": (
                        "config.host_bridge is only valid on bridge_cloud "
                        "networks; cannot set it on a non-bridge_cloud record."
                    ),
                }
        if current_type == "bridge_cloud" and "config" in patch:
            return {
                "code": 422,
                "status": "fail",
                "message": (
                    "Cannot mutate config on a bridge_cloud network; "
                    "delete and recreate to retarget the host bridge."
                ),
            }

        violation = patch_violation(network, patch)
        if violation:
            return {"code": 422, "status": "fail", "message": violation}

        before = dict(network)
        retype = new_type is not None and new_type != current_type
        # Settings and type changes take the validated path: NAT-Cloud (#320)
        # and bridge DHCP (#325) also change host state.
        validated_edit = isinstance(new_config, dict) or retype
        if not validated_edit:
            for field, value in patch.items():
                if value is not None:
                    network[field] = value
            lab.save()

    if validated_edit:
        try:
            updated, _event_type = await network_service.patch_network(
                scoped_path,
                network_id,
                patch,
                can_open=lambda other: _can_open_lab(current_user, other),
            )
        except NetworkServiceError as exc:
            body = {
                "code": exc.code,
                "status": "fail",
                "message": exc.message,
                **exc.extra,
            }
            return JSONResponse(status_code=exc.code, content=body)
        changes = audit_service.field_changes(before, updated)
        if changes:
            await audit_service.record(
                "network.update",
                actor=current_user.username,
                **audit_service.network_context(
                    data, updated, scoped_path, detail={"changes": changes}
                ),
            )
        return {
            "code": 200,
            "status": "success",
            "message": "Network updated successfully.",
            "data": updated,
            "revision": LabService.read_meta_revision_static(scoped_path),
        }

    changes = audit_service.field_changes(before, network)
    if changes:
        await audit_service.record(
            "network.update",
            actor=current_user.username,
            **audit_service.network_context(
                data, network_id, scoped_path, detail={"changes": changes}
            ),
        )
    return {
        "code": 200,
        "status": "success",
        "message": "Network updated successfully.",
        "data": network,
        # #248: post-write revision for the editor's next If-Match write.
        "revision": lab.revision,
    }


@router.delete("/{lab_path:path}/networks/{network_id}", dependencies=[LAB_ACCESS])
async def delete_network(
    lab_path: str,
    network_id: int,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    # #328: remove the network from the file as it is under the lock.
    async with _locked_lab(scoped_path) as (lab, error):
        if error is not None:
            return error
        data = lab.data
        networks = data.get("networks", {})
        if str(network_id) not in networks:
            return {
                "code": 404,
                "status": "fail",
                "message": "Network does not exist.",
            }

        networks.pop(str(network_id))
        for node in data.get("nodes", {}).values():
            for interface in node.get("interfaces", []):
                if interface.get("network_id") == network_id:
                    interface["network_id"] = 0

        data["topology"] = [
            link
            for link in data.get("topology", [])
            if link.get("network_id") != network_id
            and link.get("source") != f"network{network_id}"
            and link.get("destination") != f"network{network_id}"
        ]
        # Issue #174 follow-up: also drop v2 links[] entries referencing the
        # deleted network — write_lab_json_static no longer regenerates
        # links[] from topology[] for v2 labs.
        data["links"] = [
            link
            for link in data.get("links", []) or []
            if not (
                isinstance(link, dict)
                and (
                    (isinstance(link.get("from"), dict) and link["from"].get("network_id") == network_id)
                    or (isinstance(link.get("to"), dict) and link["to"].get("network_id") == network_id)
                )
            )
        ]
        lab.save()

    return {
        "code": 200,
        "status": "success",
        "message": "Network deleted successfully.",
        # #248: post-write revision for the editor's next If-Match write.
        "revision": lab.revision,
    }


# ---------------------------------------------------------------------------
# US-063 / US-064: per-resource node-interface PATCH/GET + bulk-PUT layout
# ---------------------------------------------------------------------------

_LAYOUT_NODE_ALLOWED = {"id", "left", "top"}
_LAYOUT_NETWORK_ALLOWED = {"id", "left", "top"}
_LAYOUT_TOP_ALLOWED = {"nodes", "networks", "viewport", "defaults"}
_LAYOUT_DEFAULTS_ALLOWED = {"link_style"}
_LAYOUT_VIEWPORT_ALLOWED = {"x", "y", "zoom"}


def _interface_network_id(lab_data: dict, node_id: int, interface_index: int) -> int:
    node = (lab_data.get("nodes") or {}).get(str(node_id)) or {}
    interfaces = node.get("interfaces") or []
    if 0 <= interface_index < len(interfaces):
        iface = interfaces[interface_index]
        if isinstance(iface, dict):
            value = iface.get("network_id", 0)
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0
    return 0


@router.patch("/{lab_path:path}/nodes/{node_id}/interfaces/{interface_index}", dependencies=[LAB_ACCESS])
async def patch_node_interface(
    lab_path: str,
    node_id: int,
    interface_index: int,
    body: dict = Body(...),
    current_user: UserRead = Depends(get_current_user),
):
    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
    except PermissionError as e:
        return {"code": 403, "status": "fail", "message": str(e)}

    normalized = _normalize_relative_lab_path(scoped_path)

    async with _locked_lab(normalized) as (lab, error):
        if error is not None:
            return error
        data = lab.data

        node = (data.get("nodes") or {}).get(str(node_id))
        if not isinstance(node, dict):
            return {"code": 404, "status": "fail", "message": "Node does not exist."}
        interfaces = node.get("interfaces") or []
        if not (0 <= interface_index < len(interfaces)):
            return {"code": 404, "status": "fail", "message": "Interface does not exist."}
        iface = interfaces[interface_index]
        if not isinstance(iface, dict):
            return {"code": 404, "status": "fail", "message": "Interface does not exist."}
        before_mac = iface.get("planned_mac")

        new_mac = body.get("planned_mac")
        if "planned_mac" in body and new_mac:
            mac_lower = str(new_mac).strip().lower()
            network_id = _interface_network_id(data, node_id, interface_index) or 0
            try:
                from app.services.mac_registry import mac_registry  # type: ignore
            except ImportError:
                mac_registry = None  # type: ignore
            if mac_registry is not None and network_id:
                conflict = mac_registry.check_collision(
                    network_id,
                    mac_lower,
                    owner_key=(normalized, int(node_id), int(interface_index)),
                )
                if conflict is not None:
                    suggested = mac_registry.suggest_mac(network_id, base_mac=mac_lower)
                    return {
                        "code": 409,
                        "status": "fail",
                        "message": "mac collision",
                        "suggested_mac": suggested,
                    }
            iface["planned_mac"] = mac_lower
        elif "planned_mac" in body and new_mac is None:
            iface["planned_mac"] = None

        if "port_position" in body:
            iface["port_position"] = body["port_position"]

        data.pop("topology", None)
        lab.save()

        from app.services.link_service import _recompute_mac_registry
        _recompute_mac_registry(normalized, data)

        derived_network_id = _interface_network_id(data, node_id, interface_index)
        response_iface = {
            "index": iface.get("index", interface_index),
            "name": iface.get("name", ""),
            "planned_mac": iface.get("planned_mac"),
            "port_position": iface.get("port_position"),
            "network_id": derived_network_id,
            "live_mac": None,
        }

    # #300: a changed MAC address is a node configuration change. A moved
    # port is layout, like a moved node, and is not an event.
    if iface.get("planned_mac") != before_mac:
        await audit_service.record(
            "node.update",
            actor=current_user.username,
            **audit_service.node_context(
                data,
                node_id,
                scoped_path,
                detail={
                    "changes": {
                        f"{iface.get('name') or f'eth{interface_index}'}.planned_mac": {
                            "from": before_mac,
                            "to": iface.get("planned_mac"),
                        }
                    }
                },
            ),
        )

    await ws_hub.publish(
        normalized,
        "interface_updated",
        {
            "node_id": int(node_id),
            "interface_index": int(interface_index),
            "interface": response_iface,
        },
    )

    return {
        "code": 200,
        "status": "success",
        "message": "Interface updated successfully.",
        "interface": response_iface,
        # #248: post-write revision for the editor's next If-Match write.
        "revision": lab.revision,
    }


@router.get("/{lab_path:path}/nodes/{node_id}/interfaces/v2", dependencies=[LAB_ACCESS])
async def list_node_interfaces_v2(
    lab_path: str,
    node_id: int,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        data = _read_lab_data(_scoped_lab_path(current_user, lab_path, treat_as_absolute=True))
    except LegacyLabSchemaError as exc:
        return _legacy_schema_response(exc)
    except FileNotFoundError:
        return {"code": 404, "status": "fail", "message": "Lab does not exist (60038)."}
    except PermissionError as e:
        return {"code": 403, "status": "fail", "message": str(e)}

    node = (data.get("nodes") or {}).get(str(node_id))
    if not isinstance(node, dict):
        return {"code": 404, "status": "fail", "message": "Node does not exist."}

    interfaces = []
    for index, iface in enumerate(node.get("interfaces", []) or []):
        if not isinstance(iface, dict):
            continue
        interfaces.append({
            "index": iface.get("index", index),
            "name": iface.get("name", ""),
            "planned_mac": iface.get("planned_mac"),
            "port_position": iface.get("port_position"),
            "network_id": _interface_network_id(data, node_id, index),
            "live_mac": None,
        })

    return {
        "code": 200,
        "status": "success",
        "message": "Successfully listed interfaces.",
        "data": interfaces,
    }


@router.get("/{lab_path:path}/nodes/{node_id}/interfaces/{idx}/live_mac", dependencies=[LAB_ACCESS])
async def get_interface_live_mac(
    lab_path: str,
    node_id: int,
    idx: int,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
        data = _read_lab_data(scoped_path)
    except LegacyLabSchemaError as exc:
        return _legacy_schema_response(exc)
    except FileNotFoundError:
        return {"code": 404, "status": "fail", "message": "Lab does not exist (60038)."}
    except PermissionError as e:
        return {"code": 403, "status": "fail", "message": str(e)}

    node = (data.get("nodes") or {}).get(str(node_id))
    if not isinstance(node, dict):
        return {"code": 404, "status": "fail", "message": "Node does not exist."}

    interfaces = node.get("interfaces") or []
    if not (0 <= idx < len(interfaces)):
        return {"code": 404, "status": "fail", "message": "Interface does not exist."}

    lab_id = str(data.get("id", "")).strip()
    runtime_service = NodeRuntimeService()
    result = runtime_service.read_live_mac(lab_id, node_id, idx, lab_data=data)

    await ws_hub.publish(
        _normalize_relative_lab_path(scoped_path),
        "interface_live_mac",
        {
            "node_id": int(node_id),
            "interface_index": int(idx),
            "state": result.get("state"),
            "planned_mac": result.get("planned_mac"),
            "live_mac": result.get("live_mac"),
            "reason": result.get("reason"),
        },
        rev=str(lab_id),
    )

    return result


def _layout_validate_and_collect_forbidden(body: dict) -> list[str]:
    forbidden: list[str] = []
    if not isinstance(body, dict):
        return ["<body>"]
    for key in body.keys():
        if key not in _LAYOUT_TOP_ALLOWED:
            forbidden.append(key)

    nodes = body.get("nodes")
    if isinstance(nodes, list):
        for entry in nodes:
            if not isinstance(entry, dict):
                forbidden.append("nodes[*]")
                continue
            for k in entry.keys():
                if k not in _LAYOUT_NODE_ALLOWED:
                    forbidden.append(f"nodes[*].{k}")
    elif nodes is not None:
        forbidden.append("nodes")

    networks = body.get("networks")
    if isinstance(networks, list):
        for entry in networks:
            if not isinstance(entry, dict):
                forbidden.append("networks[*]")
                continue
            for k in entry.keys():
                if k not in _LAYOUT_NETWORK_ALLOWED:
                    forbidden.append(f"networks[*].{k}")
    elif networks is not None:
        forbidden.append("networks")

    viewport = body.get("viewport")
    if isinstance(viewport, dict):
        for k in viewport.keys():
            if k not in _LAYOUT_VIEWPORT_ALLOWED:
                forbidden.append(f"viewport.{k}")
    elif viewport is not None:
        forbidden.append("viewport")

    defaults = body.get("defaults")
    if isinstance(defaults, dict):
        for k in defaults.keys():
            if k not in _LAYOUT_DEFAULTS_ALLOWED:
                forbidden.append(f"defaults.{k}")
    elif defaults is not None:
        forbidden.append("defaults")

    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for item in forbidden:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


@router.put("/{lab_path:path}/layout", dependencies=[LAB_ACCESS])
async def put_layout(
    lab_path: str,
    request: Request,
    body: dict = Body(...),
    current_user: UserRead = Depends(get_current_user),
):
    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
    except PermissionError as e:
        return {"code": 403, "status": "fail", "message": str(e)}

    forbidden = _layout_validate_and_collect_forbidden(body)
    if forbidden:
        return JSONResponse(
            status_code=409,
            content={
                "code": 409,
                "status": "fail",
                "message": "structural mutation forbidden",
                "forbidden_fields": forbidden,
            },
        )

    normalized = _normalize_relative_lab_path(scoped_path)

    affected_nodes: list[int] = []
    affected_networks: list[int] = []

    async with _locked_lab(normalized) as (lab, error):
        if error is not None:
            return error
        data = lab.data

        # #245: optimistic concurrency — a stale client (racing reload or a
        # second tab) must get 412 instead of silently reverting newer state.
        mismatch = _if_match_mismatch(request, data)
        if mismatch is not None:
            return JSONResponse(
                status_code=412,
                content={
                    "code": 412,
                    "status": "fail",
                    "message": "Lab changed elsewhere; reload and re-apply (stale revision).",
                    "revision": mismatch,
                },
            )

        for node_entry in body.get("nodes", []) or []:
            node_id = node_entry.get("id")
            if node_id is None:
                continue
            node = (data.get("nodes") or {}).get(str(node_id))
            if not isinstance(node, dict):
                continue
            if "left" in node_entry:
                node["left"] = int(node_entry["left"])
            if "top" in node_entry:
                node["top"] = int(node_entry["top"])
            affected_nodes.append(int(node_id))

        for net_entry in body.get("networks", []) or []:
            net_id = net_entry.get("id")
            if net_id is None:
                continue
            network = (data.get("networks") or {}).get(str(net_id))
            if not isinstance(network, dict):
                continue
            if "left" in net_entry:
                network["left"] = int(net_entry["left"])
            if "top" in net_entry:
                network["top"] = int(net_entry["top"])
            affected_networks.append(int(net_id))

        if isinstance(body.get("viewport"), dict):
            data["viewport"] = {**(data.get("viewport") or {}), **body["viewport"]}

        if isinstance(body.get("defaults"), dict):
            data["defaults"] = {**(data.get("defaults") or {}), **body["defaults"]}

        data.pop("topology", None)
        lab.save()

    await ws_hub.publish(
        normalized,
        "layout_updated",
        {
            "node_ids": affected_nodes,
            "network_ids": affected_networks,
        },
    )

    return {
        "code": 200,
        "status": "success",
        "message": "Layout updated successfully.",
        # #245: post-write revision so the client can update its cached
        # token without a re-GET.
        "revision": lab.revision,
        "data": {
            "node_ids": affected_nodes,
            "network_ids": affected_networks,
        },
    }


@router.get("/{lab_path:path}", dependencies=[LAB_ACCESS])
async def get_lab(
    lab_path: str,
    current_user: UserRead = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }
    lab_service = LabService(db)
    try:
        lab = await lab_service.get_lab_by_filename(scoped_path)
    except ValueError:
        return _json_error(
            400,
            {"code": 400, "status": "fail", "message": "Invalid or absent lab path."},
        )

    if lab:
        data = {
            "id": str(lab.id),
            "filename": lab.filename,
            "name": lab.name,
            "path": lab.path,
            "owner": lab.owner,
            "author": lab.author,
            "description": lab.description,
            "body": lab.body,
            "version": lab.version,
            "scripttimeout": lab.scripttimeout,
            "countdown": lab.countdown,
            "linkwidth": lab.linkwidth,
            "grid": lab.grid,
            "lock": lab.lock,
            "sat": "-1",
            "shared": [],
        }
        # #245: DB rows don't track the machine-owned concurrency revision —
        # read it from the lab file so DB-registered labs get If-Match
        # protection too. Files are the write-side choke point
        # (write_lab_json_static bumps meta.revision on every write).
        try:
            file_data = _read_lab_data(scoped_path)
            file_revision = (file_data.get("meta") or {}).get("revision")
            if isinstance(file_revision, int):
                data["revision"] = file_revision
            data["defaults"] = file_data.get("defaults") or {}
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError, LegacyLabSchemaError, ValueError):
            pass
        return {
            "code": 200,
            "status": "success",
            "message": "Lab has been loaded (60020).",
            "data": data,
        }

    try:
        data = _read_lab_data(scoped_path)
    except LegacyLabSchemaError as exc:
        return _legacy_schema_response(exc)
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        # NotADirectoryError fires when a stray request like
        # ``/labs/foo.json/nodes/8`` falls through to this catch-all
        # because no matching sub-route exists — the lab file is a
        # FILE, so the OS rejects the nested open. Treat as 404.
        # IsADirectoryError covers the symmetric case of pointing at
        # a directory that has no lab JSON of its own.
        return {
            "code": 404,
            "status": "fail",
            "message": "Lab does not exist (60038).",
        }

    relative_path = lab_path.strip("/").replace("\\", "/")
    # The branch above lists what it returns; this one passes the file's
    # meta through, minus the server's own id bookkeeping (#332).
    meta = {k: v for k, v in data.get("meta", {}).items() if k not in HIGH_WATER_FIELDS}
    meta["id"] = data.get("id")
    meta["filename"] = relative_path
    meta["path"] = f"/{relative_path}"
    meta["owner"] = current_user.username
    meta["defaults"] = data.get("defaults") or {}
    meta.setdefault("shared", [])

    return {
        "code": 200,
        "status": "success",
        "message": "Lab has been loaded (60020).",
        "data": meta,
    }


@router.delete("/_/{lab_path:path}", dependencies=[LAB_ACCESS])
async def delete_lab(
    lab_path: str,
    current_user: UserRead = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    lab_service = LabService(db)
    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
    except PermissionError as e:
        return {"code": 403, "status": "fail", "message": str(e)}

    try:
        async with lab_membership_lock(scoped_path, ipam=True):
            lab = await lab_service.get_lab_by_filename(scoped_path)
            if lab is not None:
                if lab.owner != current_user.username and current_user.role != "admin":
                    return {"code": 403, "status": "fail", "message": "Access denied."}
                path, name, audit_details = lab.path, lab.name, {}
                cancelled = await lab_service.delete_lab(lab, locked=True)
            else:
                # An admin may remove a file imported outside the DB-backed API.
                filepath = _lab_file_path(scoped_path)
                if not filepath.is_file():
                    return {"code": 404, "status": "fail", "message": "Lab does not exist (60038)."}
                if current_user.role != "admin":
                    return {"code": 403, "status": "fail", "message": "Access denied."}
                cancelled = await lab_service.delete_lab_file(scoped_path)
                path, name = f"/{scoped_path}", Path(scoped_path).stem
                audit_details = {"detail": {"orphan": True}}
    except SharedCloudReferenceError as e:
        return {
            "code": 409,
            "status": "fail",
            "message": e.message,
            "data": {"references": e.references},
        }
    except NodeRuntimeError as e:
        return {"code": 409, "status": "fail", "message": str(e)}

    await notify_after_mutation(
        audit_service.record(
            "lab.delete",
            actor=current_user.username,
            target_type="lab",
            target_id=path,
            target_name=name,
            lab_path=path,
            lab_name=name,
            **audit_details,
        ),
        cancelled,
    )
    return {"code": 200, "status": "success", "message": "Lab has been deleted (60023)."}


@router.post("/_/{lab_path:path}/move", dependencies=[LAB_ACCESS])
async def move_lab(
    lab_path: str,
    request: LabMoveRequest,
    current_user: UserRead = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    lab_service = LabService(db)
    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
        scoped_target = _scoped_folder_path(current_user, request.target_folder, treat_as_absolute=True)
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    try:
        source = _lab_file_path(scoped_path)
        new_relative = "/".join(part for part in [scoped_target, Path(scoped_path).name] if part)
        target = _lab_file_path(new_relative)
    except ValueError as e:
        return {
            "code": 400,
            "status": "fail",
            "message": str(e),
        }

    if not source.is_file():
        return {
            "code": 404,
            "status": "fail",
            "message": "Lab does not exist (60038).",
        }
    if target.exists():
        return {
            "code": 409,
            "status": "fail",
            "message": f"Lab already exists at target: /{new_relative} (60039).",
        }

    # Preserve deliberate creation of a missing directory, but do not
    # recreate a destination ancestor that moved while this request waited.
    target_ancestor = next(directory for directory in target.parents if directory.is_dir())
    try:
        original_directory = target_ancestor.stat()
    except (FileNotFoundError, NotADirectoryError):
        return {"code": 409, "status": "fail", "message": "The destination folder changed (60039)."}

    # Re-resolve both paths under their membership locks after any folder move.
    cancelled = False
    async with lab_membership_lock(scoped_path, new_relative):
        source = _lab_file_path(scoped_path)
        target = _lab_file_path(new_relative)
        lab = await lab_service.get_lab_by_filename(scoped_path)
        if not source.is_file():
            return {"code": 404, "status": "fail", "message": "Lab does not exist (60038)."}
        try:
            same_directory = os.path.samestat(original_directory, target_ancestor.stat())
        except (FileNotFoundError, NotADirectoryError):
            same_directory = False
        if not same_directory:
            return {"code": 409, "status": "fail", "message": "The destination folder changed (60039)."}
        try:
            with source.open(encoding="utf-8") as lab_json:
                data = json.load(lab_json)
        except (OSError, ValueError):
            data = None
        try:
            lab_id = recording_lab_id(source, data if isinstance(data, dict) else {})
            source_recordings = lab_recordings_dir(source, lab_id)
            target_recordings = lab_recordings_dir(target, lab_id)
        except ValueError as e:
            return {"code": 400, "status": "fail", "message": str(e)}
        if target.exists() or target_recordings.exists():
            return {
                "code": 409,
                "status": "fail",
                "message": "A lab or recordings folder already exists at the destination (60039).",
            }
        lab_moved = relays_relocated = False
        recorder = get_console_recorder()
        try:
            # Lazy-create the target folder, including Shared on first use.
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
            lab_moved = True
            if source_recordings.exists():
                target_recordings.parent.mkdir(parents=True, exist_ok=True)
                source_recordings.rename(target_recordings)
            recorder.relocate_lab(source, target)
            relays_relocated = True
            if lab is not None:
                lab.filename = new_relative
                lab.path = f"/{new_relative}"
                _, cancelled = await wait_for_mutation(asyncio.create_task(db.commit()))
        except (Exception, asyncio.CancelledError) as exc:
            # A previously idle console can start a recording while the DB
            # commits. Move any directory it created back with the lab.
            if lab_moved and target_recordings.exists():
                source_recordings.parent.mkdir(parents=True, exist_ok=True)
                target_recordings.rename(source_recordings)
            if lab_moved:
                shutil.move(str(target), str(source))
            if relays_relocated:
                recorder.relocate_lab(target, source)
            if isinstance(exc, asyncio.CancelledError):
                raise
            _logger.exception("move_lab: could not move %s to %s", source, target)
            return {
                "code": 500,
                "status": "fail",
                "message": "Lab move failed (60039).",
            }

    await notify_after_mutation(
        audit_service.record(
            "lab.move",
            actor=current_user.username,
            target_type="lab",
            target_id=f"/{new_relative}",
            # The lab row's name, falling back to the file stem for imports.
            target_name=getattr(lab, "name", None) or source.stem,
            lab_path=f"/{new_relative}",
            detail={"from": f"/{scoped_path}", "to": f"/{new_relative}"},
        ),
        cancelled,
    )
    return {
        "code": 200,
        "status": "success",
        "message": "Lab has been moved (60039).",
        "data": {"path": f"/{new_relative}"},
    }
