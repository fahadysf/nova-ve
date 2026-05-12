# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
import copy
import logging
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.config import get_settings
from app.dependencies import get_current_user
from app.schemas.lab import LabMetaCreate, LabMetaUpdate
from app.schemas.network import NetworkCreate, NetworkUpdate
from app.schemas.node import (
    NodeBatchCreate,
    NodeCreate,
    NodeFromPairedTemplate,
    NodeFromTemplate,
    NodeUpdate,
)
from app.schemas.user import UserRead
from app.services.guacamole_db_service import GuacamoleDatabaseError, GuacamoleDatabaseService
from app.services.html5_service import Html5SessionError, Html5SessionService
from app.services.lab_lock import lab_lock
from app.services.lab_service import LEGACY_SCHEMA_ERROR, LabService, _lab_file_path, _normalize_relative_lab_path
from app.services.node_runtime_service import NodeRuntimeError, NodeRuntimeService
from app.services.template_service import (
    TemplateError,
    TemplateService,
    _icon_filename_for,
    normalize_paired_child_kind,
    render_interface_name,
)
from app.services.ws_hub import ws_hub

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/labs", tags=["labs"])


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

NODE_FIELDS_EDITABLE_WHILE_RUNNING = {"name", "icon", "left", "top", "interface_naming_scheme"}
NODE_FIELDS_EDITABLE_WHILE_STOPPED = {"image", "cpu", "ram", "ethernet", "console", "delay", "extras"}
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


def _user_root(current_user: UserRead) -> str:
    if getattr(current_user, "role", "admin") == "admin":
        return ""
    return getattr(current_user, "folder", "/").strip().replace("\\", "/").strip("/")


def _scoped_lab_path(current_user: UserRead, raw_path: str, treat_as_absolute: bool) -> str:
    normalized = raw_path.strip().replace("\\", "/").strip("/")
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


def _default_interfaces(
    node_type: str,
    ethernet_count: int,
    interface_naming_scheme: str | None = None,
    template_interface_naming: dict | None = None,
) -> list[dict]:
    """Generate default interface list for a node.

    Priority (highest first):
    1. ``interface_naming_scheme`` — node-level override (US-105)
    2. ``template_interface_naming["format"]`` — template-level format string
    3. Hard-coded fallback: ``Gi{port}`` for qemu, ``eth{n}`` for everything else
    """
    interfaces = []
    for index in range(ethernet_count):
        if interface_naming_scheme is not None:
            name = render_interface_name(interface_naming_scheme, index)
        elif template_interface_naming and "format" in template_interface_naming:
            name = render_interface_name(template_interface_naming["format"], index)
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


def _first_mac_for_node(node_id: int) -> str:
    return f"50:00:00:{node_id:02x}:00:00"


def _html5_launcher_url(lab_path: str, node_id: int) -> str:
    quoted_path = quote(lab_path.strip("/"), safe="/")
    return f"/api/labs/{quoted_path}/nodes/{node_id}/html5"


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
    requested_fields = set(request.model_dump(exclude_unset=True).keys())
    invalid_fields = requested_fields - NODE_FIELDS_MUTABLE
    if invalid_fields:
        invalid_list = ", ".join(sorted(invalid_fields))
        return f"Unsupported node field update: {invalid_list}."

    if node_running:
        blocked_fields = sorted(field for field in requested_fields if field in NODE_FIELDS_EDITABLE_WHILE_STOPPED)
        if blocked_fields:
            blocked_list = ", ".join(blocked_fields)
            return f"Stop the node before changing: {blocked_list}."

    if "image" in requested_fields:
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

    if request.type == "qemu":
        if not merged_extras.get("uuid"):
            merged_extras["uuid"] = str(uuid.uuid4())
        if not merged_extras.get("firstmac"):
            merged_extras["firstmac"] = _first_mac_for_node(node_id)

    return {
        "id": node_id,
        "name": name,
        "type": request.type,
        "template": request.template,
        "image": request.image,
        "console": request.console if "console" in provided_fields else template.console_type,
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
        "icon": request.icon if "icon" in provided_fields and request.icon else _icon_filename_for(template.icon_type),
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


@router.post("/")
async def create_lab(
    request: LabMetaCreate,
    current_user: UserRead = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    lab_service = LabService(db)
    try:
        scoped_path = _scoped_lab_path(current_user, request.path or "", treat_as_absolute=False)
        lab = await lab_service.create_lab(
            owner=current_user.username,
            name=request.name,
            path=scoped_path,
            filename=request.filename,
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
        )
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


@router.put("/{lab_path:path}/meta")
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
    lab = await lab_service.get_lab_by_filename(scoped_path)

    if not lab:
        return {
            "code": 404,
            "status": "fail",
            "message": "Lab does not exist (60038).",
        }

    if lab.owner != current_user.username and current_user.role != "admin":
        return {
            "code": 403,
            "status": "fail",
            "message": "Access denied.",
        }

    lab = await lab_service.update_lab(lab, **request.model_dump(exclude_unset=True))
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


@router.get("/{lab_path:path}/topology")
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


@router.put("/{lab_path:path}/topology")
async def update_topology(
    lab_path: str,
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

    settings = get_settings()
    normalized = _normalize_relative_lab_path(scoped_path)

    # Issue #174 follow-up: serialize topology writes against link_service /
    # network_service via lab_lock. Without this, two concurrent writers can
    # interleave and lose mutations. Plus, the post-write reconcile below has
    # to see the same JSON state we just wrote — read inside the lock.
    with lab_lock(normalized, settings.LABS_DIR):
        try:
            data = _read_lab_data(scoped_path)
        except LegacyLabSchemaError as exc:
            return _legacy_schema_response(exc)
        except FileNotFoundError:
            return {
                "code": 404,
                "status": "fail",
                "message": "Lab does not exist (60038).",
            }

        if isinstance(payload, list):
            data["topology"] = payload
        else:
            data["topology"] = payload.get("topology", data.get("topology", []))
            for node_id, node_patch in payload.get("nodes", {}).items():
                node = data.get("nodes", {}).get(str(node_id))
                if node and isinstance(node_patch, dict):
                    for field, value in node_patch.items():
                        if value is not None:
                            node[field] = value
            for network_id, network_patch in payload.get("networks", {}).items():
                network = data.get("networks", {}).get(str(network_id))
                if network and isinstance(network_patch, dict):
                    for field, value in network_patch.items():
                        if value is not None:
                            network[field] = value

        LabService.write_lab_json_static(scoped_path, data)

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

    return {
        "code": 200,
        "status": "success",
        "message": "Topology saved successfully.",
        "data": data.get("topology", []),
    }


@router.get("/{lab_path:path}/nodes")
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


@router.get("/{lab_path:path}/node-catalog")
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


@router.post("/{lab_path:path}/nodes")
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

    nodes = data.setdefault("nodes", {})
    next_id = max((int(node_key) for node_key in nodes.keys()), default=0) + 1
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

    node = _build_node_payload(
        node_id=next_id,
        request=request,
        template=template,
        name=request.name,
        left=request.left,
        top=request.top,
    )
    nodes[str(next_id)] = node
    LabService.write_lab_json_static(scoped_path, data)

    return {
        "code": 200,
        "status": "success",
        "message": "Node created successfully.",
        "data": node,
    }


@router.post("/{lab_path:path}/nodes/from-template")
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
    node_create = NodeCreate(
        name=request.name,
        type=request.template_type,
        template=request.template_key,
        image=request.image,
        console=request.console or template_def.console_type or "telnet",
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

    if child_type == "qemu":
        if not extras.get("uuid"):
            extras["uuid"] = str(uuid.uuid4())
        if not extras.get("firstmac"):
            extras["firstmac"] = _first_mac_for_node(node_id)

    template_iface_naming = child.get("interface_naming")
    if not isinstance(template_iface_naming, dict):
        template_iface_naming = None

    interfaces = _default_interfaces(
        child_type,
        ethernet,
        interface_naming_scheme=None,
        template_interface_naming=template_iface_naming,
    )

    explicit_names = (template_iface_naming or {}).get("explicit") if template_iface_naming else None
    if isinstance(explicit_names, list):
        for idx, iface in enumerate(interfaces):
            if idx < len(explicit_names) and isinstance(explicit_names[idx], str):
                iface["name"] = explicit_names[idx]

    from app.services.template_service import synthetic_paired_child_key

    return {
        "id": node_id,
        "name": name,
        "type": child_type,
        "template": synthetic_paired_child_key(template_key, child_id),
        "image": str(child.get("image") or ""),
        "console": str(child.get("console") or "telnet"),
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
        "icon": _icon_filename_for(str(child.get("icon_type") or "router")),
        "width": "0",
        "config": False,
        "config_list": [],
        "sat": 0,
        "computed_sat": 0,
        "interfaces": interfaces,
        "extras": extras,
    }


@router.post("/{lab_path:path}/nodes/from-paired-template")
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

    settings = get_settings()
    normalized = _normalize_relative_lab_path(scoped_path)

    from app.services.link_service import LinkService

    link_service = LinkService()
    created_nodes: list[dict] = []
    created_links: list[dict] = []

    # #208a — hold lab_lock across snapshot+phase1+phase2+restore so a
    # concurrent writer cannot slip in between snapshot and restore (which
    # would silently clobber their work). LinkService.create_link normally
    # acquires its own lab_lock; we pass _lab_lock_held=True so the inner
    # call uses a nullcontext (fcntl flock is not reentrant on Linux).
    with lab_lock(normalized, settings.LABS_DIR):
        try:
            data = _read_lab_data(scoped_path)
        except LegacyLabSchemaError as exc:
            return _json_error(422, _legacy_schema_response(exc))
        except FileNotFoundError:
            return _json_error(
                404,
                {
                    "code": 404,
                    "status": "fail",
                    "message": "Lab does not exist (60038).",
                },
            )

        snapshot = copy.deepcopy(data)
        nodes_map = data.setdefault("nodes", {})
        next_id = max((int(node_key) for node_key in nodes_map.keys()), default=0) + 1

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
                payload = _build_paired_child_payload(
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
                next_id += 1

            LabService.write_lab_json_static(scoped_path, data)
        except (ValueError, TemplateError) as exc:
            # #208-MEDIUM — bad child scalars (e.g. ``ethernet: "not-an-int"``)
            # and malformed-child entries are template defects, not server
            # bugs. Surface as 422 so the operator sees a fix-the-template
            # message instead of a generic 500. Pre-flight already catches
            # link/iface defects; this catches the scalar shape that pre-flight
            # doesn't (and shouldn't, to keep validator scope tight).
            LabService.write_lab_json_static(scoped_path, snapshot)
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
            LabService.write_lab_json_static(scoped_path, snapshot)
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
            LabService.write_lab_json_static(scoped_path, snapshot)
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
    }


@router.post("/{lab_path:path}/nodes/batch")
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

    nodes = data.setdefault("nodes", {})
    next_id = max((int(node_key) for node_key in nodes.keys()), default=0) + 1
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

    created_nodes = []
    for index in range(request.count):
        node_id = next_id + index
        left, top = _node_position(index, request.left, request.top, request.placement)
        node = _build_node_payload(
            node_id=node_id,
            request=request,
            template=template,
            name=f"{request.name_prefix}-{index + 1}",
            left=left,
            top=top,
        )
        nodes[str(node_id)] = node
        created_nodes.append(node)

    LabService.write_lab_json_static(scoped_path, data)
    return {
        "code": 200,
        "status": "success",
        "message": "Nodes created successfully.",
        "data": {
            "nodes": created_nodes,
        },
    }


@router.get("/{lab_path:path}/nodes/{node_id}/interfaces")
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


@router.get("/{lab_path:path}/nodes/{node_id}")
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


@router.put("/{lab_path:path}/nodes/{node_id}")
async def update_node(
    lab_path: str,
    node_id: int,
    request: NodeUpdate,
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

    for field, value in request.model_dump(exclude_unset=True).items():
        if value is not None:
            node[field] = value

    node["interfaces"] = _resize_interfaces(
        node.get("interfaces", []),
        node.get("type", "qemu"),
        int(node.get("ethernet", 0)),
    )
    LabService.write_lab_json_static(scoped_path, data)

    lab_id = str(data.get("id", "")).strip()
    enriched = NodeRuntimeService().enrich_node(lab_id, node_id, node) if lab_id else node

    return {
        "code": 200,
        "status": "success",
        "message": "Node updated successfully.",
        "data": enriched,
    }


@router.delete("/{lab_path:path}/nodes/{node_id}")
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

    try:
        NodeRuntimeService().stop_node(data, node_id)
    except NodeRuntimeError:
        pass

    nodes.pop(str(node_id))
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
    LabService.write_lab_json_static(scoped_path, data)

    return {
        "code": 200,
        "status": "success",
        "message": "Node deleted successfully.",
    }


@router.get("/{lab_path:path}/nodes/{node_id}/start")
async def start_node(
    lab_path: str,
    node_id: int,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        data = _read_lab_data(_scoped_lab_path(current_user, lab_path, treat_as_absolute=True))
        # The runtime backends are synchronous and can block for tens
        # of seconds (Dynamips IOS boot, QEMU image-copy at first boot,
        # Docker image pull). Run them in a worker thread so the
        # event loop stays free for the rest of the request mix.
        await asyncio.to_thread(NodeRuntimeService().start_node, data, node_id)
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

    return {
        "code": 200,
        "status": "success",
        "message": "Node started (80049).",
    }


@router.get("/{lab_path:path}/nodes/{node_id}/stop")
async def stop_node(
    lab_path: str,
    node_id: int,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        data = _read_lab_data(_scoped_lab_path(current_user, lab_path, treat_as_absolute=True))
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

    return {
        "code": 200,
        "status": "success",
        "message": "Node stopped (80050).",
    }


@router.get("/{lab_path:path}/nodes/{node_id}/wipe")
async def wipe_node(
    lab_path: str,
    node_id: int,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        data = _read_lab_data(_scoped_lab_path(current_user, lab_path, treat_as_absolute=True))
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

    return {
        "code": 200,
        "status": "success",
        "message": "Node cleared (80053).",
    }


@router.get("/{lab_path:path}/nodes/{node_id}/logs")
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


@router.get("/{lab_path:path}/nodes/{node_id}/console")
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


@router.get("/{lab_path:path}/nodes/{node_id}/qemu-preview")
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


@router.get("/{lab_path:path}/nodes/{node_id}/telnet")
async def node_telnet(
    lab_path: str,
    node_id: int,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        data = _read_lab_data(_scoped_lab_path(current_user, lab_path, treat_as_absolute=True))
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

    body = f"telnet://{console['host']}:{console['port']}\n"
    response = PlainTextResponse(body, media_type="application/x-telnet")
    response.headers["Content-Disposition"] = f'attachment; filename="node-{node_id}.telnet"'
    return response


@router.get("/{lab_path:path}/nodes/{node_id}/rdp")
async def node_rdp(
    lab_path: str,
    node_id: int,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        data = _read_lab_data(_scoped_lab_path(current_user, lab_path, treat_as_absolute=True))
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


@router.get("/{lab_path:path}/nodes/{node_id}/html5")
async def node_html5(
    lab_path: str,
    node_id: int,
    current_user: UserRead = Depends(get_current_user),
):
    if not getattr(current_user, "html5", True):
        return {
            "code": 403,
            "status": "fail",
            "message": "HTML5 console access is disabled for this user.",
        }

    try:
        data = _read_lab_data(_scoped_lab_path(current_user, lab_path, treat_as_absolute=True))
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

    try:
        settings = get_settings()
        if settings.GUACAMOLE_DATABASE_URL.strip():
            html5_url = await GuacamoleDatabaseService().create_console_url(
                current_user,
                host=console["host"],
                port=console["port"],
                protocol=console["console"],
                connection_name=console["name"],
                connection_key=f"{data.get('id', lab_path)}:{node_id}:{console['console']}",
            )
        else:
            html5_url = await Html5SessionService().create_console_url(
                current_user,
                host=console["host"],
                port=console["port"],
                protocol=console["console"],
                connection_name=console["name"],
            )
    except (Html5SessionError, GuacamoleDatabaseError) as exc:
        return {
            "code": 500,
            "status": "fail",
            "message": str(exc),
        }

    return RedirectResponse(url=html5_url, status_code=307)


@router.get("/{lab_path:path}/networks")
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


@router.post("/{lab_path:path}/networks")
async def create_network(
    lab_path: str,
    request: NetworkCreate,
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

    networks = data.setdefault("networks", {})
    next_id = max((int(network_key) for network_key in networks.keys()), default=0) + 1
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
    LabService.write_lab_json_static(scoped_path, data)
    network["count"] = 0

    return {
        "code": 200,
        "status": "success",
        "message": "Network created successfully.",
        "data": network,
    }


@router.put("/{lab_path:path}/networks/{network_id}")
async def update_network(
    lab_path: str,
    network_id: int,
    request: NetworkUpdate,
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

    network = data.get("networks", {}).get(str(network_id))
    if not network:
        return {
            "code": 404,
            "status": "fail",
            "message": "Network does not exist.",
        }

    for field, value in request.model_dump(exclude_unset=True).items():
        if value is not None:
            network[field] = value

    LabService.write_lab_json_static(scoped_path, data)
    return {
        "code": 200,
        "status": "success",
        "message": "Network updated successfully.",
        "data": network,
    }


@router.delete("/{lab_path:path}/networks/{network_id}")
async def delete_network(
    lab_path: str,
    network_id: int,
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
    LabService.write_lab_json_static(scoped_path, data)

    return {
        "code": 200,
        "status": "success",
        "message": "Network deleted successfully.",
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


@router.patch("/{lab_path:path}/nodes/{node_id}/interfaces/{interface_index}")
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
    settings = get_settings()

    with lab_lock(normalized, settings.LABS_DIR):
        try:
            data = _read_lab_data(normalized)
        except LegacyLabSchemaError as exc:
            return _legacy_schema_response(exc)
        except FileNotFoundError:
            return {"code": 404, "status": "fail", "message": "Lab does not exist (60038)."}

        node = (data.get("nodes") or {}).get(str(node_id))
        if not isinstance(node, dict):
            return {"code": 404, "status": "fail", "message": "Node does not exist."}
        interfaces = node.get("interfaces") or []
        if not (0 <= interface_index < len(interfaces)):
            return {"code": 404, "status": "fail", "message": "Interface does not exist."}
        iface = interfaces[interface_index]
        if not isinstance(iface, dict):
            return {"code": 404, "status": "fail", "message": "Interface does not exist."}

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
        LabService.write_lab_json_static(normalized, data)

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
    }


@router.get("/{lab_path:path}/nodes/{node_id}/interfaces/v2")
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


@router.get("/{lab_path:path}/nodes/{node_id}/interfaces/{idx}/live_mac")
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
        lab_id,
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


@router.put("/{lab_path:path}/layout")
async def put_layout(
    lab_path: str,
    body: dict = Body(...),
    current_user: UserRead = Depends(get_current_user),
):
    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
    except PermissionError as e:
        return {"code": 403, "status": "fail", "message": str(e)}

    forbidden = _layout_validate_and_collect_forbidden(body)
    if forbidden:
        from fastapi.responses import JSONResponse

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
    settings = get_settings()

    affected_nodes: list[int] = []
    affected_networks: list[int] = []

    with lab_lock(normalized, settings.LABS_DIR):
        try:
            data = _read_lab_data(normalized)
        except LegacyLabSchemaError as exc:
            return _legacy_schema_response(exc)
        except FileNotFoundError:
            return {"code": 404, "status": "fail", "message": "Lab does not exist (60038)."}

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
        LabService.write_lab_json_static(normalized, data)

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
        "data": {
            "node_ids": affected_nodes,
            "network_ids": affected_networks,
        },
    }


@router.get("/{lab_path:path}")
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
    lab = await lab_service.get_lab_by_filename(scoped_path)

    if lab:
        return {
            "code": 200,
            "status": "success",
            "message": "Lab has been loaded (60020).",
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
                "sat": "-1",
                "shared": [],
            },
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
    meta = data.get("meta", {})
    meta["id"] = data.get("id")
    meta["filename"] = relative_path
    meta["path"] = f"/{relative_path}"
    meta["owner"] = current_user.username
    meta.setdefault("shared", [])

    return {
        "code": 200,
        "status": "success",
        "message": "Lab has been loaded (60020).",
        "data": meta,
    }


@router.delete("/_/{lab_path:path}")
async def delete_lab(
    lab_path: str,
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
    lab = await lab_service.get_lab_by_filename(scoped_path)

    if lab:
        if lab.owner != current_user.username and current_user.role != "admin":
            return {
                "code": 403,
                "status": "fail",
                "message": "Access denied.",
            }
        await lab_service.delete_lab(lab)
        return {
            "code": 200,
            "status": "success",
            "message": "Lab has been deleted (60023).",
        }

    # Filesystem orphan: lab.json exists in LABS_DIR but no DB row (e.g. left
    # behind by a partial delete or imported via a non-API path). Admins may
    # clean these up; non-admins fall back to the existing 404 response.
    try:
        filepath = _lab_file_path(scoped_path)
    except ValueError:
        filepath = None

    if filepath is not None and filepath.is_file():
        if current_user.role != "admin":
            return {
                "code": 403,
                "status": "fail",
                "message": "Access denied.",
            }
        filepath.unlink()
        return {
            "code": 200,
            "status": "success",
            "message": "Lab has been deleted (60023).",
        }

    return {
        "code": 404,
        "status": "fail",
        "message": "Lab does not exist (60038).",
    }
