# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Lab management API (#262).

Operator view over lab instances: every lab with state + allocated
resources, plus lab-wide stop/wipe lifecycle actions.

* ``GET  /api/manage/labs``                       — listing (admins see
  everything; non-admins are scoped to their folder root, mirroring the
  folders router).
* ``POST /api/manage/labs/{lab_path}/stop``       — stop every running node.
* ``POST /api/manage/labs/{lab_path}/wipe``       — stop + wipe every node.

Deliberately NOT mounted under ``/api/labs``: the labs router's
``/{lab_path:path}`` catch-all would shadow (or be shadowed by) a listing
route there.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends

from app.dependencies import get_current_user
from app.openapi import COMMON_RESPONSES
from app.schemas.user import UserRead
from app.services.lab_lock import LabLockTimeout, lab_lock
from app.services.lab_service import LabService
from app.services.node_runtime_service import NodeRuntimeService

_logger = logging.getLogger("nova-ve.manage")

router = APIRouter(
    prefix="/api/manage/labs",
    tags=["manage"],
    responses=COMMON_RESPONSES,
)

nodes_router = APIRouter(
    prefix="/api/manage/nodes",
    tags=["manage"],
    responses=COMMON_RESPONSES,
)


def _ok(message: str, data: Any) -> dict:
    return {"code": 200, "status": "success", "message": message, "data": data}


def _fail(message: str, *, code: int = 400) -> dict:
    return {"code": code, "status": "fail", "message": message, "data": None}


def _resolve_lab_file(labs_dir: Path, lab_path: str) -> Path | None:
    """Resolve a lab path (with or without leading slash / .json suffix)."""
    cleaned = lab_path.strip().replace("\\", "/").strip("/")
    if not cleaned:
        return None
    if not cleaned.endswith(".json"):
        cleaned = f"{cleaned}.json"
    candidate = (labs_dir / cleaned).resolve()
    labs_root = labs_dir.resolve()
    if labs_root not in candidate.parents and candidate.parent != labs_root:
        return None
    return candidate if candidate.exists() else None


def _user_folder_root(current_user: UserRead) -> str:
    if getattr(current_user, "role", "admin") == "admin":
        return ""
    return str(getattr(current_user, "folder", "/") or "/").replace("\\", "/").strip("/")


def _summarize_lab(lab_file: Path, labs_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(lab_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    try:
        relative = lab_file.relative_to(labs_dir).as_posix()
    except ValueError:
        return None

    nodes = data.get("nodes")
    if not isinstance(nodes, dict):
        nodes = {}
    running = 0
    cpu_alloc = 0
    ram_alloc = 0
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        try:
            if int(node.get("status", 0)) == 2:
                running += 1
        except (TypeError, ValueError):
            pass
        try:
            cpu_alloc += int(node.get("cpu", 1))
        except (TypeError, ValueError):
            cpu_alloc += 1
        try:
            ram_alloc += int(node.get("ram", 0))
        except (TypeError, ValueError):
            pass

    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    return {
        "path": f"/{relative}",
        "file": lab_file.name,
        "name": str(meta.get("name") or lab_file.stem),
        "author": str(meta.get("author") or ""),
        "description": str(meta.get("description") or ""),
        "state": "running" if running > 0 else "stopped",
        "nodes_total": len(nodes),
        "nodes_running": running,
        "cpu_allocated": cpu_alloc,
        "ram_allocated_mb": ram_alloc,
        "lock": bool(meta.get("lock")),
    }


@router.get("")
async def list_managed_labs(
    current_user: UserRead = Depends(get_current_user),
):
    """Every lab instance with state and allocated resources.

    Admins see the whole tree; non-admins only labs under their folder
    root (same tenancy scoping as the folder browser).
    """
    labs_dir = _labs_dir_from_config()
    root = _user_folder_root(current_user)

    labs: list[dict[str, Any]] = []
    if labs_dir.exists():
        for lab_file in sorted(labs_dir.rglob("*.json")):
            summary = _summarize_lab(lab_file, labs_dir)
            if summary is None:
                continue
            if root and not summary["path"].lstrip("/").startswith(f"{root}/"):
                continue
            labs.append(summary)

    totals = {
        "labs": len(labs),
        "running": sum(1 for lab in labs if lab["state"] == "running"),
        "nodes_total": sum(lab["nodes_total"] for lab in labs),
        "nodes_running": sum(lab["nodes_running"] for lab in labs),
        "cpu_allocated": sum(lab["cpu_allocated"] for lab in labs),
        "ram_allocated_mb": sum(lab["ram_allocated_mb"] for lab in labs),
    }
    return _ok("Managed labs listed.", {"labs": labs, "totals": totals})


def _labs_dir_from_config():
    from app.config import get_settings

    return get_settings().LABS_DIR


def _stop_or_wipe(lab_path: str, wipe: bool) -> dict:
    """Shared lifecycle action: stop (optionally wipe) every node."""
    from app.services.node_runtime_service import NodeRuntimeError

    labs_dir = _labs_dir_from_config()
    lab_file = _resolve_lab_file(labs_dir, lab_path)
    if lab_file is None:
        return _fail(f"Lab not found: {lab_path}", code=404)

    runtime = NodeRuntimeService()
    stopped: list[int] = []
    wiped: list[int] = []
    failed: list[dict[str, Any]] = []
    try:
        with lab_lock(lab_file.relative_to(labs_dir).as_posix(), labs_dir):
            data = LabService.read_lab_json_static(
                lab_file.relative_to(labs_dir).as_posix()
            )
            nodes = data.get("nodes") or {}
            for node_id_raw in sorted(nodes.keys()):
                node = nodes.get(node_id_raw)
                if not isinstance(node, dict):
                    continue
                try:
                    node_id = int(node_id_raw)
                except (TypeError, ValueError):
                    continue
                try:
                    status = int(node.get("status", 0) or 0)
                except (TypeError, ValueError):
                    status = 0
                if not wipe and status != 2:
                    continue
                try:
                    if wipe:
                        runtime.wipe_node(data, node_id)
                        wiped.append(node_id)
                    else:
                        runtime.stop_node(data, node_id)
                        stopped.append(node_id)
                except NodeRuntimeError as exc:
                    failed.append({"node_id": node_id, "error": str(exc)})
    except LabLockTimeout:
        return _fail("Lab is busy; retry in a moment.", code=503)
    except FileNotFoundError:
        return _fail(f"Lab not found: {lab_path}", code=404)

    return _ok(
        "Lab wiped." if wipe else "Lab stopped.",
        {
            "lab": f"/{lab_file.relative_to(labs_dir).as_posix()}",
            "stopped": stopped,
            "wiped": wiped,
            "failed": failed,
        },
    )


@router.post("/{lab_path:path}/stop")
async def stop_lab(
    lab_path: str,
    current_user: UserRead = Depends(get_current_user),
):
    if current_user.role != "admin":
        return _fail("Admin role required.", code=403)
    return _stop_or_wipe(lab_path, wipe=False)


@router.post("/{lab_path:path}/wipe")
async def wipe_lab(
    lab_path: str,
    current_user: UserRead = Depends(get_current_user),
):
    if current_user.role != "admin":
        return _fail("Admin role required.", code=403)
    return _stop_or_wipe(lab_path, wipe=True)


# ----------------------------------------------------------------------
# Node management (#263): every node across labs with live usage.
# ----------------------------------------------------------------------
@nodes_router.get("")
async def list_managed_nodes(
    state: str = "running",
    lab: str = "",
    template: str = "",
    node_type: str = "",
    current_user: UserRead = Depends(get_current_user),
):
    """Cross-lab node inventory with live usage from the runtime registry.

    ``state`` filters running|stopped|all (default running); ``lab`` matches
    the lab file name, ``template`` the node template, ``node_type`` the
    runtime kind (qemu/docker/iol/dynamips). Admin-only: this view spans
    every lab regardless of folder tenancy.
    """
    if current_user.role != "admin":
        return _fail("Admin role required.", code=403)

    labs_dir = _labs_dir_from_config()
    runtime = NodeRuntimeService()

    state_norm = state.strip().lower()
    if state_norm not in {"running", "stopped", "all"}:
        return _fail("state must be one of running|stopped|all.")
    lab_needle = lab.strip().lower()
    template_needle = template.strip().lower()
    type_needle = node_type.strip().lower()

    rows: list[dict[str, Any]] = []
    if labs_dir.exists():
        for lab_file in sorted(labs_dir.rglob("*.json")):
            try:
                data = json.loads(lab_file.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            lab_id = str(data.get("id", "")).strip()
            if not lab_id:
                continue
            try:
                lab_path = f"/{lab_file.relative_to(labs_dir).as_posix()}"
            except ValueError:
                continue
            lab_name = str((data.get("meta") or {}).get("name") or lab_file.stem)
            if lab_needle and lab_needle not in lab_file.name.lower() and lab_needle not in lab_name.lower():
                continue
            nodes = data.get("nodes")
            if not isinstance(nodes, dict):
                continue
            for node_id_raw, node in nodes.items():
                if not isinstance(node, dict):
                    continue
                if template_needle and template_needle not in str(node.get("template", "")).lower():
                    continue
                if type_needle and type_needle not in str(node.get("type", "")).lower():
                    continue
                try:
                    node_id = int(node_id_raw)
                except (TypeError, ValueError):
                    continue
                enriched = runtime.enrich_node(lab_id, node_id, node)
                status = int(enriched.get("status", 0) or 0)
                is_running = status == 2
                if state_norm == "running" and not is_running:
                    continue
                if state_norm == "stopped" and is_running:
                    continue
                rows.append(
                    {
                        "lab_path": lab_path,
                        "lab_name": lab_name,
                        "node_id": node_id,
                        "name": str(node.get("name") or f"node-{node_id}"),
                        "type": str(node.get("type") or ""),
                        "template": str(node.get("template") or ""),
                        "image": str(node.get("image") or ""),
                        "status": status,
                        "state": "running" if is_running else "stopped",
                        "console": str(node.get("console") or ""),
                        "url": enriched.get("url"),
                        "cpu_allocated": node.get("cpu"),
                        "ram_allocated_mb": node.get("ram"),
                        "cpu_usage": enriched.get("cpu_usage", 0),
                        "ram_usage": enriched.get("ram_usage", 0),
                        "disk_usage": enriched.get("disk_usage", 0),
                    }
                )

    totals = {
        "nodes": len(rows),
        "running": sum(1 for row in rows if row["state"] == "running"),
        "cpu_allocated": sum(int(row.get("cpu_allocated") or 0) for row in rows),
        "ram_allocated_mb": sum(int(row.get("ram_allocated_mb") or 0) for row in rows),
    }
    return _ok("Managed nodes listed.", {"nodes": rows, "totals": totals})
