# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse, RedirectResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.config import get_settings
from app.dependencies import get_current_user
from app.schemas.lab import LabMetaCreate, LabMetaUpdate
from app.schemas.network import NetworkCreate, NetworkUpdate
from app.schemas.node import NodeCreate, NodeUpdate
from app.schemas.user import UserRead
from app.services.guacamole_db_service import GuacamoleDatabaseError, GuacamoleDatabaseService
from app.services.html5_service import Html5SessionError, Html5SessionService
from app.services.lab_service import LabService
from app.services.node_runtime_service import NodeRuntimeError, NodeRuntimeService
from app.services.template_service import TemplateError, TemplateService

router = APIRouter(prefix="/api/labs", tags=["labs"])


def _read_lab_data(lab_path: str) -> dict:
    return LabService.read_lab_json_static(lab_path)


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
        interfaces.append({"name": name, "network_id": 0})
    return interfaces


def _resize_interfaces(existing: list[dict], node_type: str, ethernet_count: int) -> list[dict]:
    resized = _default_interfaces(node_type, ethernet_count)
    for index, interface in enumerate(existing[:ethernet_count]):
        resized[index]["name"] = interface.get("name", resized[index]["name"])
        resized[index]["network_id"] = interface.get("network_id", 0)
    return resized


def _first_mac_for_node(node_id: int) -> str:
    return f"50:00:00:{node_id:02x}:00:00"


def _html5_launcher_url(lab_path: str, node_id: int) -> str:
    quoted_path = quote(lab_path.strip("/"), safe="/")
    return f"/api/labs/{quoted_path}/nodes/{node_id}/html5"


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


@router.put("/{lab_path:path}")
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


@router.delete("/{lab_path:path}")
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


@router.get("/{lab_path:path}/topology")
async def get_topology(
    lab_path: str,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        data = _read_lab_data(_scoped_lab_path(current_user, lab_path, treat_as_absolute=True))
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
    payload: dict | list,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
        data = _read_lab_data(scoped_path)
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


@router.post("/{lab_path:path}/nodes")
async def create_node(
    lab_path: str,
    request: NodeCreate,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        scoped_path = _scoped_lab_path(current_user, lab_path, treat_as_absolute=True)
        data = _read_lab_data(scoped_path)
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

    provided_fields = request.model_fields_set
    node = {
        "id": next_id,
        "name": request.name,
        "type": request.type,
        "template": request.template,
        "image": request.image,
        "console": request.console if "console" in provided_fields else template.console,
        "status": 0,
        "delay": 0,
        "cpu": request.cpu if "cpu" in provided_fields else template.cpu,
        "ram": request.ram if "ram" in provided_fields else template.ram,
        "ethernet": request.ethernet if "ethernet" in provided_fields else template.ethernet,
        "cpulimit": template.cpulimit,
        "uuid": str(uuid.uuid4()) if request.type == "qemu" else None,
        "firstmac": _first_mac_for_node(next_id) if request.type == "qemu" else None,
        "left": request.left,
        "top": request.top,
        "icon": template.icon,
        "width": "0",
        "config": False,
        "config_list": [],
        "sat": 0,
        "computed_sat": 0,
        "interfaces": _default_interfaces(
            request.type,
            request.ethernet if "ethernet" in provided_fields else template.ethernet,
        ),
    }
    nodes[str(next_id)] = node
    LabService.write_lab_json_static(scoped_path, data)

    return {
        "code": 200,
        "status": "success",
        "message": "Node created successfully.",
        "data": node,
    }


@router.get("/{lab_path:path}/nodes/{node_id}/interfaces")
async def list_interfaces(
    lab_path: str,
    node_id: int,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        data = _read_lab_data(_scoped_lab_path(current_user, lab_path, treat_as_absolute=True))
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
        "visibility": 1,
        "smart": -1,
        "count": 0,
    }
    networks[str(next_id)] = network
    LabService.write_lab_json_static(scoped_path, data)

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
