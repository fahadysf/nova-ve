# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Per-resource /links endpoints (US-063 + US-064)."""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Header
from fastapi.responses import JSONResponse

from app.dependencies import get_current_user
from app.schemas.user import UserRead
from app.services.lab_service import LEGACY_SCHEMA_ERROR
from app.services.link_service import (
    DuplicateLinkError,
    LinkContentionError,
    link_service,
)


router = APIRouter(prefix="/api/labs", tags=["links"])


def _legacy_schema_response(lab_path: str) -> dict:
    return {
        "code": 422,
        "status": "fail",
        "message": LEGACY_SCHEMA_ERROR,
        "lab_path": lab_path,
    }


def _read_lab_or_error(lab_path: str):
    """Light wrapper to surface the same envelope as legacy routers."""
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


@router.get("/{lab_path:path}/links")
async def list_links(
    lab_path: str,
    current_user: UserRead = Depends(get_current_user),
):
    data, err = _read_lab_or_error(lab_path)
    if err is not None:
        return err
    return {
        "code": 200,
        "status": "success",
        "message": "Successfully listed links.",
        "data": link_service.list_links(lab_path),
    }


@router.post("/{lab_path:path}/links")
async def create_link(
    lab_path: str,
    body: Dict[str, Any] = Body(...),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    current_user: UserRead = Depends(get_current_user),
):
    data, err = _read_lab_or_error(lab_path)
    if err is not None:
        return err

    from_endpoint = body.get("from")
    to_endpoint = body.get("to")
    if from_endpoint is None or to_endpoint is None:
        return JSONResponse(
            status_code=400,
            content={
                "code": 400,
                "status": "fail",
                "message": "from and to endpoints are required",
            },
        )

    style_override = body.get("style_override")
    try:
        link_payload, network_payload, replayed = await link_service.create_link(
            lab_path,
            from_endpoint,
            to_endpoint,
            style_override=style_override,
            idempotency_key=idempotency_key,
        )
    except DuplicateLinkError as exc:
        return JSONResponse(
            status_code=409,
            content={
                "code": 409,
                "status": "fail",
                "message": "link already exists",
                "existing_link": exc.existing_link,
            },
        )
    except LinkContentionError as exc:
        # US-303 codex iter1 MEDIUM: bounded mutex wait expired (default
        # 2.0s); surface as 409 so the client can retry deliberately.
        return JSONResponse(
            status_code=409,
            content={
                "code": 409,
                "status": "fail",
                "message": str(exc),
            },
        )
    except KeyError as exc:
        return JSONResponse(
            status_code=404,
            content={
                "code": 404,
                "status": "fail",
                "message": str(exc),
            },
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={
                "code": 400,
                "status": "fail",
                "message": str(exc),
            },
        )

    response: Dict[str, Any] = {
        "code": 200 if replayed else 201,
        "status": "success",
        "message": "Link replayed (idempotent)." if replayed else "Link created successfully.",
        "link": link_payload,
    }
    if network_payload is not None:
        response["network"] = network_payload

    return JSONResponse(status_code=200 if replayed else 201, content=response)


@router.delete("/{lab_path:path}/links/{link_id}")
async def delete_link(
    lab_path: str,
    link_id: str,
    current_user: UserRead = Depends(get_current_user),
):
    data, err = _read_lab_or_error(lab_path)
    if err is not None:
        return err

    # Determine if the link exists *before* mutation so we can branch the response shape.
    pre_links = data.get("links", []) or []
    existed = any(str(l.get("id")) == str(link_id) for l in pre_links)

    try:
        await link_service.delete_link(lab_path, link_id)
    except LinkContentionError as exc:
        # US-303 codex iter1 MEDIUM: bounded mutex wait expired.
        return JSONResponse(
            status_code=409,
            content={
                "code": 409,
                "status": "fail",
                "message": str(exc),
            },
        )

    if not existed:
        return {
            "code": 200,
            "status": "success",
            "already_deleted": True,
            "message": "Link not found; treated as already deleted.",
        }

    return {
        "code": 200,
        "status": "success",
        "message": "Link deleted successfully.",
    }


@router.patch("/{lab_path:path}/links/{link_id}")
async def patch_link(
    lab_path: str,
    link_id: str,
    body: Dict[str, Any] = Body(...),
    current_user: UserRead = Depends(get_current_user),
):
    data, err = _read_lab_or_error(lab_path)
    if err is not None:
        return err

    updated = await link_service.patch_link(lab_path, link_id, body)
    if updated is None:
        return JSONResponse(
            status_code=404,
            content={
                "code": 404,
                "status": "fail",
                "message": "Link does not exist.",
            },
        )

    return {
        "code": 200,
        "status": "success",
        "message": "Link updated successfully.",
        "link": updated,
    }
