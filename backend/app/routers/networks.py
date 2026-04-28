# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Per-resource /networks endpoints (US-063 + US-064).

These endpoints replace/augment the legacy ones in ``routers.labs``. They are
registered under the same prefix; the legacy ones remain in place but are
shadowed for the verbs they share. PATCH is new and does not collide with the
legacy PUT.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse

from app.dependencies import get_current_user
from app.schemas.network import NetworkCreate, NetworkUpdate
from app.schemas.user import UserRead
from app.services.lab_service import LEGACY_SCHEMA_ERROR
from app.services.network_service import NetworkServiceError, network_service


router = APIRouter(prefix="/api/labs", tags=["networks"])


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


@router.get("/{lab_path:path}/networks")
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


@router.post("/{lab_path:path}/networks")
async def create_network(
    lab_path: str,
    request: NetworkCreate,
    current_user: UserRead = Depends(get_current_user),
):
    data, err = _read_lab_or_error(lab_path)
    if err is not None:
        return err

    try:
        payload = await network_service.create_network(lab_path, request.model_dump())
    except NetworkServiceError as exc:
        body: Dict[str, Any] = {
            "code": exc.code,
            "status": "fail",
            "message": exc.message,
        }
        body.update(exc.extra)
        return JSONResponse(status_code=exc.code, content=body)
    return JSONResponse(
        status_code=201,
        content={
            "code": 201,
            "status": "success",
            "message": "Network created successfully.",
            "network": payload,
        },
    )


@router.delete("/{lab_path:path}/networks/{network_id}")
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

    return {
        "code": 200,
        "status": "success",
        "message": "Network deleted successfully.",
    }


@router.patch("/{lab_path:path}/networks/{network_id}")
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

    return {
        "code": 200,
        "status": "success",
        "message": "Network updated successfully.",
        "event": event_type,
        "network": payload,
    }
