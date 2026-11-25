# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Per-resource /networks endpoints (US-063 + US-064).

These endpoints replace/augment the legacy ones in ``routers.labs``. They are
registered under the same prefix; the legacy ones remain in place but are
shadowed for the verbs they share. PATCH is new and does not collide with the
legacy PUT.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse

from app.dependencies import get_current_user
from app.openapi import COMMON_RESPONSES
from app.routers.labs import LAB_ACCESS, _scoped_lab_path
from app.schemas.network import NATCloudValidate, NetworkCreate, NetworkSharingUpdate, NetworkUpdate
from app.schemas.user import UserRead
from app.services import audit_service
from app.services.host_net import HostNetBridgeOwnershipError
from app.services.lab_service import LEGACY_SCHEMA_ERROR, LabService
from app.services.network_info_service import build_network_info
from app.services.network_service import NetworkServiceError, network_service


router = APIRouter(prefix="/api/labs", tags=["networks"], responses=COMMON_RESPONSES)


def _legacy_schema_response(lab_path: str) -> dict:
    return {
        "code": 422,
        "status": "fail",
        "message": LEGACY_SCHEMA_ERROR,
        "lab_path": lab_path,
    }


def _read_lab_or_error(lab_path: str):
    from app.services.lab_service import LabService

    try:
        return LabService.read_lab_json_static(lab_path), None
    except ValueError as exc:
        message = str(exc)
        if message.startswith(LEGACY_SCHEMA_ERROR):
            return None, JSONResponse(status_code=200, content=_legacy_schema_response(lab_path))
        raise
    except FileNotFoundError:
        return None, JSONResponse(
            status_code=200,
            content={
                "code": 404,
                "status": "fail",
                "message": "Lab does not exist (60038).",
            },
        )


def _can_open(user: UserRead, lab_path: str) -> bool:
    try:
        _scoped_lab_path(user, lab_path, treat_as_absolute=True)
    except PermissionError:
        return False
    return True


@router.get("/{lab_path:path}/networks", dependencies=[LAB_ACCESS])
async def list_networks(
    lab_path: str,
    include_hidden: bool = Query(default=False),
    current_user: UserRead = Depends(get_current_user),
):
    data, err = _read_lab_or_error(lab_path)
    if err is not None:
        return err

    return {
        "code": 200,
        "status": "success",
        "message": "Successfully listed networks (60004).",
        "data": network_service.list_networks(lab_path, include_hidden=include_hidden),
    }


@router.post("/{lab_path:path}/networks", dependencies=[LAB_ACCESS])
async def create_network(
    lab_path: str,
    request: NetworkCreate,
    current_user: UserRead = Depends(get_current_user),
):
    data, err = _read_lab_or_error(lab_path)
    if err is not None:
        return err

    try:
        payload = await network_service.create_network(
            lab_path,
            request.model_dump(),
            # Same audience as the NAT-Cloud inventory in /api/system/clouds.
            allow_unshared_clouds=current_user.role == "admin",
            can_open=lambda other: _can_open(current_user, other),
        )
    except HostNetBridgeOwnershipError as exc:
        return JSONResponse(
            status_code=409,
            content={
                "code": 409,
                "status": "fail",
                "message": str(exc),
            },
        )
    except NetworkServiceError as exc:
        body: Dict[str, Any] = {
            "code": exc.code,
            "status": "fail",
            "message": exc.message,
        }
        body.update(exc.extra)
        return JSONResponse(status_code=exc.code, content=body)
    # #297: the lab was read before the network existed — pass its record.
    await audit_service.record(
        "network.create",
        actor=current_user.username,
        **audit_service.network_context(data, payload, lab_path),
    )
    return JSONResponse(
        status_code=201,
        content={
            "code": 201,
            "status": "success",
            "message": "Network created successfully.",
            "network": payload,
            # #248: post-write revision for the editor's next If-Match write.
            "revision": LabService.read_meta_revision_static(lab_path),
        },
    )


@router.post("/{lab_path:path}/networks/validate-nat-cloud", dependencies=[LAB_ACCESS])
async def validate_nat_cloud_config(
    lab_path: str,
    request: NATCloudValidate,
    current_user: UserRead = Depends(get_current_user),
):
    _data, err = _read_lab_or_error(lab_path)
    if err is not None:
        return err
    try:
        normalized = await asyncio.to_thread(
            network_service.validate_nat_cloud_config,
            lab_path,
            request.config,
            network_id=request.network_id,
            can_open=lambda other: _can_open(current_user, other),
        )
    except NetworkServiceError as exc:
        body: Dict[str, Any] = {
            "code": exc.code,
            "status": "fail",
            "message": exc.message,
        }
        body.update(exc.extra)
        return JSONResponse(status_code=exc.code, content=body)
    return {
        "code": 200,
        "status": "success",
        "data": {"config": normalized},
    }


@router.delete("/{lab_path:path}/networks/{network_id}", dependencies=[LAB_ACCESS])
async def delete_network(
    lab_path: str,
    network_id: int,
    current_user: UserRead = Depends(get_current_user),
):
    data, err = _read_lab_or_error(lab_path)
    if err is not None:
        return err

    try:
        await network_service.delete_network(lab_path, network_id)
    except NetworkServiceError as exc:
        body: Dict[str, Any] = {
            "code": exc.code,
            "status": "fail",
            "message": exc.message,
        }
        body.update(exc.extra)
        return JSONResponse(status_code=exc.code, content=body)

    # #297: describe the network while it is still in the pre-delete read.
    await audit_service.record(
        "network.delete",
        actor=current_user.username,
        **audit_service.network_context(data, network_id, lab_path),
    )

    return {
        "code": 200,
        "status": "success",
        "message": "Network deleted successfully.",
        # #248: post-write revision for the editor's next If-Match write.
        "revision": LabService.read_meta_revision_static(lab_path),
    }


@router.patch("/{lab_path:path}/networks/{network_id}", dependencies=[LAB_ACCESS])
async def patch_network(
    lab_path: str,
    network_id: int,
    request: NetworkUpdate,
    current_user: UserRead = Depends(get_current_user),
):
    data, err = _read_lab_or_error(lab_path)
    if err is not None:
        return err

    try:
        payload, event_type = await network_service.patch_network(
            lab_path,
            network_id,
            request.model_dump(exclude_unset=True),
        )
    except NetworkServiceError as exc:
        body: Dict[str, Any] = {
            "code": exc.code,
            "status": "fail",
            "message": exc.message,
        }
        body.update(exc.extra)
        return JSONResponse(status_code=exc.code, content=body)

    # #297: ``data`` still holds the pre-patch network record. A patch that
    # changed nothing is not an event worth a row. The event describes the
    # network as it is now, so the payload is layered over the old record.
    previous = data.get("networks", {}).get(str(network_id)) or {}
    payload = payload if isinstance(payload, dict) else {}
    changes = audit_service.field_changes(dict(previous), payload)
    if changes:
        await audit_service.record(
            "network.update",
            actor=current_user.username,
            **audit_service.network_context(
                data, {**previous, **payload}, lab_path, detail={"changes": changes}
            ),
        )

    return {
        "code": 200,
        "status": "success",
        "message": "Network updated successfully.",
        "event": event_type,
        "network": payload,
        # #248: post-write revision for the editor's next If-Match write.
        "revision": LabService.read_meta_revision_static(lab_path),
    }


def _service_error_response(exc: NetworkServiceError) -> JSONResponse:
    body: Dict[str, Any] = {"code": exc.code, "status": "fail", "message": exc.message}
    body.update(exc.extra)
    return JSONResponse(status_code=exc.code, content=body)


@router.get("/{lab_path:path}/networks/{network_id}/info", dependencies=[LAB_ACCESS])
async def get_network_info(
    lab_path: str,
    network_id: int,
    current_user: UserRead = Depends(get_current_user),
):
    """Everything the info panel shows for one network (#318).

    Read-only. Combines the stored record with live host state, and degrades
    field by field when the bridge, DHCP server or lease file is absent.
    """
    _data, err = _read_lab_or_error(lab_path)
    if err is not None:
        return err
    try:
        info = await asyncio.to_thread(
            build_network_info,
            lab_path,
            network_id,
            lambda other: _can_open(current_user, other),
        )
    except NetworkServiceError as exc:
        return _service_error_response(exc)
    return {
        "code": 200,
        "status": "success",
        "message": "Network details loaded.",
        "data": info,
    }


@router.put("/{lab_path:path}/networks/{network_id}/sharing", dependencies=[LAB_ACCESS])
async def set_network_sharing(
    lab_path: str,
    network_id: int,
    request: NetworkSharingUpdate,
    current_user: UserRead = Depends(get_current_user),
):
    """Offer a network to other labs, or stop offering it (#319)."""
    data, err = _read_lab_or_error(lab_path)
    if err is not None:
        return err
    try:
        payload, changed = await network_service.set_sharing(lab_path, network_id, request.shared)
    except NetworkServiceError as exc:
        return _service_error_response(exc)
    if changed:
        await audit_service.record(
            "network.update",
            actor=current_user.username,
            **audit_service.network_context(
                data,
                payload,
                lab_path,
                detail={"changes": {"shared": {"from": not request.shared, "to": request.shared}}},
            ),
        )
    return {
        "code": 200,
        "status": "success",
        "message": (
            "Network is now shared with other labs."
            if request.shared
            else "Network is no longer shared."
        ),
        "network": payload,
        # #248: post-write revision for the editor's next If-Match write.
        "revision": LabService.read_meta_revision_static(lab_path),
    }


@router.post("/{lab_path:path}/runtime/reconcile", dependencies=[LAB_ACCESS])
async def reconcile_runtime(
    lab_path: str,
    current_user: UserRead = Depends(get_current_user),
):
    """Idempotently ensure host bridges for every network in the lab.

    Called by the canvas on lab open so labs restored from static JSON
    or labs whose bridges were lost (host reboot, manual removal) find
    their host state already provisioned by the time the user starts a
    node or creates a link.

    #277: the walk shells out to the privileged helper per host op, which
    is far too slow to run on the event loop — a node-dense lab froze
    every concurrent request for ~30s. It runs in a worker thread; all
    state it touches is guarded by threading locks (lab_lock, the runtime
    registry mutex, the per-node runtime mutex).
    """
    data, err = _read_lab_or_error(lab_path)
    if err is not None:
        return err

    summary = await asyncio.to_thread(network_service.ensure_lab_bridges, lab_path)
    return {
        "code": 200,
        "status": "success",
        "message": "Runtime reconciliation completed.",
        "data": summary,
    }
