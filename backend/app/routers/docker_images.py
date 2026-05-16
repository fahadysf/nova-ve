# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Admin API for curating the lab-available Docker image set.

The node-creation modal only shows images that carry a ``nova-ve-lab/`` marker
tag. This router lets an admin browse local images, mark/unmark them for lab
use, and pull new ones from a registry.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.dependencies import get_current_admin, get_current_user
from app.schemas.user import UserRead
from app.services.docker_image_service import DockerImageError, DockerImageService

router = APIRouter(prefix="/api/docker/images", tags=["docker-images"])


class _ImageRefBody(BaseModel):
    image: str = Field(..., min_length=1, description="Docker image reference, e.g. 'alpine:3.22'.")


class _PullBody(BaseModel):
    reference: str = Field(..., min_length=1, description="Pullable image reference.")
    mark: bool = Field(True, description="Apply the lab marker tag after a successful pull.")


def _ok(message: str, data) -> dict:
    return {"code": 200, "status": "success", "message": message, "data": data}


def _fail(message: str, *, code: int = 400) -> dict:
    return {"code": code, "status": "fail", "message": message, "data": None}


@router.get("")
async def list_images(
    current_user: UserRead = Depends(get_current_user),
):
    """List every local Docker image, with marker state per image ID."""
    service = DockerImageService()
    records = service.list_all_images()
    return _ok(
        "Docker images listed.",
        {
            "images": [record.to_dict() for record in records],
            "marker_namespace": "nova-ve-lab",
        },
    )


@router.post("/mark")
async def mark_image(
    body: _ImageRefBody,
    _: UserRead = Depends(get_current_admin),
):
    try:
        marker = DockerImageService().mark(body.image)
    except DockerImageError as exc:
        return _fail(str(exc))
    return _ok(f"Marked {body.image} for lab use.", {"reference": body.image, "marker": marker})


@router.post("/unmark")
async def unmark_image(
    body: _ImageRefBody,
    _: UserRead = Depends(get_current_admin),
):
    try:
        marker = DockerImageService().unmark(body.image)
    except DockerImageError as exc:
        return _fail(str(exc))
    return _ok(
        f"Unmarked {body.image} (image data preserved).",
        {"reference": body.image, "marker_removed": marker},
    )


@router.post("/pull")
async def pull_image(
    body: _PullBody,
    _: UserRead = Depends(get_current_admin),
):
    try:
        result = DockerImageService().pull(body.reference, mark_after=body.mark)
    except DockerImageError as exc:
        return _fail(str(exc))
    return _ok(f"Pulled {result['reference']}.", result)
