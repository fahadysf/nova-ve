# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Dynamips runtime support: list images + idle-PC calibration.

The calibration endpoint is **synchronous** and blocks for roughly 90
seconds per call (the IOS image needs that long to settle before
``vm extract_idle_pc`` produces useful candidates). Clients are expected
to surface that latency in the UI with a spinner; HTTP timeouts on
intermediate proxies must be at least 120s.

Concurrency: the calibration boots a real VM that consumes CPU and
RAM. Concurrent calibrations would compete for host resources and
confuse the hypervisor's idle-PC analyser. The launcher's internal
lock serialises the create/start/extract/destroy cycle, so two
parallel POSTs queue up rather than fight.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import get_current_user
from app.schemas.user import UserRead
from app.services.runtime.dynamips import (
    DynamipsError,
    DynamipsLauncher,
    list_dynamips_images,
)


router = APIRouter(prefix="/api/dynamips", tags=["dynamips"])


class DynamipsImage(BaseModel):
    image: str
    path: str
    size_bytes: int
    platform: str | None
    image_sha256: str
    calibrated: bool
    idle_pc: str | None


class CalibrateRequest(BaseModel):
    image: str = Field(..., description="Image filename (basename) to calibrate.")


class CalibrateResult(BaseModel):
    image: str
    image_sha256: str
    idle_pc: str
    candidates: list[str]
    duration_s: float
    platform: str


@router.get("/images")
async def list_images(
    current_user: UserRead = Depends(get_current_user),
) -> dict:
    """List every Dynamips image present on disk with its calibration
    status. Use this to drive a "Calibrate" button in the node-edit
    modal for any image where ``calibrated`` is false.
    """
    # SHA-256 over every image file is filesystem-bound (tens to
    # hundreds of ms each for an unpacked IOS .bin); hand it to a
    # worker thread so the event loop stays free.
    data = await asyncio.to_thread(list_dynamips_images)
    return {
        "code": 200,
        "status": "success",
        "message": "Listed dynamips images.",
        "data": [DynamipsImage(**entry).model_dump() for entry in data],
    }


@router.post("/calibrate")
async def calibrate(
    body: CalibrateRequest,
    current_user: UserRead = Depends(get_current_user),
) -> dict:
    """Synchronously calibrate the idle-PC value for one image.

    Boots a throwaway Dynamips VM against the image, waits ~90s for
    IOS to settle, harvests candidates via the hypervisor's
    ``vm extract_idle_pc`` command, caches the first candidate under
    the image's SHA-256, and returns the chosen value plus the full
    candidate list.

    Caller can override the choice by writing to the template's
    ``extras.idlepc`` field — that always wins over the cache.
    """
    image_path = _resolve_image(body.image)
    try:
        # Calibration sleeps ~90s while IOS boots; running it on the
        # event loop would block every other request the worker is
        # juggling. Hand it to the default thread executor so uvicorn
        # stays responsive.
        result = await asyncio.to_thread(
            DynamipsLauncher.instance().calibrate_image, image_path
        )
    except DynamipsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "code": 200,
        "status": "success",
        "message": (
            f"Calibrated {body.image}: idle_pc={result['idle_pc']} "
            f"(took {result['duration_s']}s)."
        ),
        "data": CalibrateResult(**result).model_dump(),
    }


def _resolve_image(image_basename: str) -> Path:
    """Map a bare basename onto the on-disk image, honouring both the
    flat and EVE-NG per-image-subdir layouts the launcher accepts.
    """
    if not image_basename or "/" in image_basename or image_basename.startswith("."):
        raise HTTPException(status_code=400, detail="invalid image name")
    try:
        return DynamipsLauncher._resolve_image_path({"image": image_basename})
    except DynamipsError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
