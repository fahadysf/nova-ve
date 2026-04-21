from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.dependencies import get_current_user
from app.schemas.user import UserRead
import json
from pathlib import Path
from app.config import get_settings

router = APIRouter(prefix="/api/labs", tags=["labs"])


def _lab_path(filename: str) -> Path:
    return get_settings().LABS_DIR / filename


@router.get("/{lab_path:path}")
async def get_lab(
    lab_path: str,
    current_user: UserRead = Depends(get_current_user),
):
    filepath = _lab_path(lab_path)
    if not filepath.exists():
        return {
            "code": 404,
            "status": "fail",
            "message": "Lab does not exist (60038).",
        }

    with open(filepath, "r") as f:
        data = json.load(f)

    meta = data.get("meta", {})
    meta["id"] = data.get("id")
    meta["filename"] = filepath.name
    meta["path"] = str(filepath)
    meta["owner"] = current_user.username

    return {
        "code": 200,
        "status": "success",
        "message": "Lab has been loaded (60020).",
        "data": meta,
    }


@router.get("/{lab_path:path}/topology")
async def get_topology(
    lab_path: str,
    current_user: UserRead = Depends(get_current_user),
):
    filepath = _lab_path(lab_path)
    if not filepath.exists():
        return {
            "code": 404,
            "status": "fail",
            "message": "Lab does not exist (60038).",
        }

    with open(filepath, "r") as f:
        data = json.load(f)

    return {
        "code": 200,
        "status": "success",
        "message": "Topology loaded",
        "data": data.get("topology", []),
    }


@router.get("/{lab_path:path}/nodes")
async def list_nodes(
    lab_path: str,
    current_user: UserRead = Depends(get_current_user),
):
    filepath = _lab_path(lab_path)
    if not filepath.exists():
        return {
            "code": 404,
            "status": "fail",
            "message": "Lab does not exist (60038).",
        }

    with open(filepath, "r") as f:
        data = json.load(f)

    return {
        "code": 200,
        "status": "success",
        "message": "Successfully listed nodes (60026).",
        "data": data.get("nodes", {}),
    }


@router.get("/{lab_path:path}/nodes/{node_id}/interfaces")
async def list_interfaces(
    lab_path: str,
    node_id: int,
    current_user: UserRead = Depends(get_current_user),
):
    filepath = _lab_path(lab_path)
    if not filepath.exists():
        return {
            "code": 404,
            "status": "fail",
            "message": "Lab does not exist (60038).",
        }

    with open(filepath, "r") as f:
        data = json.load(f)

    nodes = data.get("nodes", {})
    node = nodes.get(str(node_id), {})

    return {
        "code": 200,
        "status": "success",
        "message": "Successfully listed node interfaces (60030).",
        "data": {
            "id": node_id,
            "sort": node.get("type", "qemu"),
            "ethernet": [
                {"name": iface.get("name", f"eth{i}"), "network_id": iface.get("network_id", 0)}
                for i, iface in enumerate(node.get("interfaces", []))
            ],
            "serial": [],
        },
    }


@router.get("/{lab_path:path}/nodes/{node_id}/start")
async def start_node(
    lab_path: str,
    node_id: int,
    current_user: UserRead = Depends(get_current_user),
):
    # TODO: implement actual subprocess start
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
    # TODO: implement actual subprocess stop
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
    # TODO: implement overlay deletion
    return {
        "code": 200,
        "status": "success",
        "message": "Node cleared (80053).",
    }


@router.get("/{lab_path:path}/networks")
async def list_networks(
    lab_path: str,
    current_user: UserRead = Depends(get_current_user),
):
    filepath = _lab_path(lab_path)
    if not filepath.exists():
        return {
            "code": 404,
            "status": "fail",
            "message": "Lab does not exist (60038).",
        }

    with open(filepath, "r") as f:
        data = json.load(f)

    return {
        "code": 200,
        "status": "success",
        "message": "Successfully listed networks (60004).",
        "data": data.get("networks", {}),
    }
