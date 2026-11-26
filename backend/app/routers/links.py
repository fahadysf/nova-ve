# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Per-resource /links endpoints (US-063 + US-064)."""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Header
from fastapi.responses import JSONResponse

from app.dependencies import get_current_user
from app.openapi import COMMON_RESPONSES
from app.routers.labs import LAB_ACCESS
from app.schemas.user import UserRead
from app.services import audit_service, host_net
from app.services.lab_service import LEGACY_SCHEMA_ERROR, LabIdExhaustedError, LabService
from app.services.link_service import (
    DuplicateLinkError,
    InterfaceAlreadyAttachedError,
    LinkContentionError,
    link_service,
)
from app.services.node_runtime_service import NodeRuntimeError


router = APIRouter(prefix="/api/labs", tags=["links"], responses=COMMON_RESPONSES)


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


@router.get("/{lab_path:path}/links", dependencies=[LAB_ACCESS])
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


@router.post("/{lab_path:path}/links", dependencies=[LAB_ACCESS])
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
    except InterfaceAlreadyAttachedError as exc:
        return JSONResponse(
            status_code=409,
            content={
                "code": 409,
                "status": "fail",
                "message": str(exc),
                "existing_link": exc.existing_link,
                "node_id": exc.node_id,
                "interface_index": exc.interface_index,
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
    except LabIdExhaustedError as exc:
        # #332: a node-to-node link needs a new implicit network, and the
        # lab has used every network id the host layer can name.
        return JSONResponse(
            status_code=exc.code,
            content={
                "code": exc.code,
                "status": "fail",
                "message": exc.message,
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
    except (NodeRuntimeError, host_net.HostNetError) as exc:
        # Hot-attach refused or kernel-side bridge/veth/TAP work failed.
        # Surface the real reason instead of a bare 500 so the canvas can
        # show an actionable toast (e.g. ownership mismatch, bridge
        # provisioning failure, runtime not in expected state).
        return JSONResponse(
            status_code=422,
            content={
                "code": 422,
                "status": "fail",
                "message": str(exc),
            },
        )

    if not replayed:
        # #297: an idempotent replay created nothing, so it earns no entry.
        # ``data`` predates the link, but its endpoints are older objects.
        await audit_service.record(
            "link.create",
            actor=current_user.username,
            **audit_service.link_context(data, link_payload, lab_path),
        )

    response: Dict[str, Any] = {
        "code": 200 if replayed else 201,
        "status": "success",
        "message": "Link replayed (idempotent)." if replayed else "Link created successfully.",
        "link": link_payload,
    }
    if network_payload is not None:
        response["network"] = network_payload
    # #248: the create bumped meta.revision server-side; report it so the
    # editor's next If-Match write doesn't spuriously 412.
    response["revision"] = LabService.read_meta_revision_static(lab_path)

    return JSONResponse(status_code=200 if replayed else 201, content=response)


@router.delete("/{lab_path:path}/links/{link_id}", dependencies=[LAB_ACCESS])
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
    except (NodeRuntimeError, host_net.HostNetError) as exc:
        return JSONResponse(
            status_code=422,
            content={
                "code": 422,
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

    # #297: ``data`` is the pre-delete read, so the link record is still
    # there to describe what was just removed.
    removed = next(
        (link for link in pre_links if str(link.get("id")) == str(link_id)), None
    )
    await audit_service.record(
        "link.delete",
        actor=current_user.username,
        **audit_service.link_context(data, removed, lab_path),
    )

    return {
        "code": 200,
        "status": "success",
        "message": "Link deleted successfully.",
        # #248: post-write revision for the editor's next If-Match write.
        "revision": LabService.read_meta_revision_static(lab_path),
    }


@router.patch("/{lab_path:path}/links/{link_id}", dependencies=[LAB_ACCESS])
async def patch_link(
    lab_path: str,
    link_id: str,
    body: Dict[str, Any] = Body(...),
    current_user: UserRead = Depends(get_current_user),
):
    data, err = _read_lab_or_error(lab_path)
    if err is not None:
        return err

    try:
        updated = await link_service.patch_link(lab_path, link_id, body)
    except (NodeRuntimeError, host_net.HostNetError) as exc:
        return JSONResponse(
            status_code=422,
            content={
                "code": 422,
                "status": "fail",
                "message": str(exc),
            },
        )
    if updated is None:
        return JSONResponse(
            status_code=404,
            content={
                "code": 404,
                "status": "fail",
                "message": "Link does not exist.",
            },
        )

    # #297: ``data`` is the pre-patch read; compare it with the new payload.
    # A patch that changed nothing is not an event worth a row.
    previous = next(
        (
            link
            for link in (data.get("links", []) or [])
            if str(link.get("id")) == str(link_id)
        ),
        None,
    )
    changes = audit_service.field_changes(dict(previous or {}), updated)
    if changes:
        await audit_service.record(
            "link.update",
            actor=current_user.username,
            **audit_service.link_context(
                data, updated, lab_path, detail={"changes": changes}
            ),
        )

    return {
        "code": 200,
        "status": "success",
        "message": "Link updated successfully.",
        "link": updated,
        # #248: post-write revision for the editor's next If-Match write.
        "revision": LabService.read_meta_revision_static(lab_path),
    }
