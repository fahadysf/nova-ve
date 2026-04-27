# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

import uuid
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends
from fastapi.responses import PlainTextResponse, RedirectResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.config import get_settings
from app.dependencies import get_current_user
from app.schemas.lab import LabMetaCreate, LabMetaUpdate
from app.schemas.network import NetworkCreate, NetworkUpdate
from app.schemas.node import NodeBatchCreate, NodeCreate, NodeUpdate
from app.schemas.user import UserRead
from app.services.guacamole_db_service import GuacamoleDatabaseError, GuacamoleDatabaseService
from app.services.html5_service import Html5SessionError, Html5SessionService
from app.services.lab_service import LEGACY_SCHEMA_ERROR, LabService
from app.services.node_runtime_service import NodeRuntimeError, NodeRuntimeService
from app.services.template_service import TemplateError, TemplateService, _icon_filename_for

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

NODE_FIELDS_EDITABLE_WHILE_RUNNING = {"name", "icon", "left", "top"}
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


def _default_interfaces(node_type: str, ethernet_count: int) -> list[dict]:
    interfaces = []
    for index in range(ethernet_count):
        if node_type == "qemu":
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
        "interfaces": _default_interfaces(request.type, ethernet),
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

    return {
        "code": 200,
        "status": "success",
        "message": "Node updated successfully.",
        "data": node,
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
        NodeRuntimeService().start_node(data, node_id)
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
            f"prompt for credentials:i:1",
            f"administrative session:i:0",
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
    LabService.write_lab_json_static(scoped_path, data)

    return {
        "code": 200,
        "status": "success",
        "message": "Network deleted successfully.",
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
    except FileNotFoundError:
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

    await lab_service.delete_lab(lab)
    return {
        "code": 200,
        "status": "success",
        "message": "Lab has been deleted (60023).",
    }
