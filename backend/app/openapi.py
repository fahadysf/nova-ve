# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""OpenAPI / Swagger metadata for the nova-ve backend.

Everything here is purely descriptive: it influences the generated
``/openapi.json`` and the ``/docs`` and ``/redoc`` browsers but does not
change request handling or response shapes.  Pulling the per-tag
descriptions, app-level metadata, and shared error responses into one
module keeps ``main.py`` short and keeps each router decorator focused
on its own behaviour.

See ``docs/development/backend.md`` for the human-facing API guide and
``docs/operations/vmware-deployment.md`` for the deployment notes that
the Swagger UI links to.
"""

from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel, Field

from app.schemas.common import ApiResponse


class ErrorEnvelope(BaseModel):
    """Shape returned by the backend for non-2xx responses.

    Mirrors the success envelope (``ApiResponse``) so clients can use a
    single decoder for both cases.  Fields kept optional so the schema
    matches FastAPI's default ``HTTPException`` rendering as well as the
    router-side ``JSONResponse(content={...})`` calls.
    """

    code: int = Field(..., examples=[401])
    status: str = Field(..., examples=["fail"])
    message: str = Field(..., examples=["Authentication required."])
    detail: Any = Field(None, description="Optional extra debug context.")


# Tag groups for Swagger UI navigation.  The order here is the order
# Swagger renders them, so put the most operator-facing groups first.
OPENAPI_TAGS: list[Dict[str, Any]] = [
    {
        "name": "auth",
        "description": (
            "Username/password login, session-cookie lifecycle, and "
            "self-service registration. Most other endpoints require "
            "a valid session cookie obtained from `POST /api/auth/login`."
        ),
    },
    {
        "name": "users",
        "description": "Admin user management — list, create, update, delete user accounts.",
    },
    {
        "name": "labs",
        "description": (
            "Lab CRUD and per-lab node lifecycle: create/update topology, "
            "spawn nodes from templates, start/stop/wipe runtimes, and pull "
            "telnet/RDP/HTML5 console URLs. Lab paths are URL-encoded "
            "relative paths under the labs directory."
        ),
    },
    {
        "name": "networks",
        "description": (
            "Per-lab network resource: bridges, NAT clouds, and Bridge-Cloud "
            "host-bridges. See `docs/operations/vmware-deployment.md` for "
            "the VDS portgroup requirements when Bridge-Cloud networks are "
            "used on a VMware hypervisor."
        ),
    },
    {
        "name": "links",
        "description": "Per-lab links between node interfaces and networks.",
    },
    {
        "name": "folders",
        "description": "Folder hierarchy for organising labs in the UI.",
    },
    {
        "name": "list",
        "description": "Template catalogue and per-template artefacts (images).",
    },
    {
        "name": "docker-images",
        "description": (
            "Admin curation of the lab-available Docker image set. Only "
            "images tagged with the `nova-ve-lab/` marker show up in the "
            "node-creation modal."
        ),
    },
    {
        "name": "dynamips",
        "description": (
            "Dynamips (Cisco IOS) runtime support: image catalog and "
            "synchronous idle-PC calibration. Calibration blocks for ~90 s "
            "per call."
        ),
    },
    {
        "name": "system",
        "description": "Health, version, and host status endpoints.",
    },
    {
        "name": "websocket",
        "description": "WebSocket endpoints for live runtime telemetry and console streams.",
    },
]


# Shared error responses to spread into router constructors so the spec
# is honest about which non-2xx codes endpoints may return.  These are
# documentation-only — they do not affect runtime behaviour.
def _err(code: int, description: str) -> Dict[str, Any]:
    return {"model": ErrorEnvelope, "description": description}


COMMON_RESPONSES: Dict[int | str, Dict[str, Any]] = {
    401: _err(401, "Authentication required or session expired."),
    403: _err(403, "Authenticated user lacks the required role."),
    404: _err(404, "Target resource (lab, node, network, link, etc.) not found."),
    422: _err(422, "Request body or parameters failed validation."),
    503: _err(
        503,
        "Lab is locked by a concurrent operation; clients should "
        "retry after the `Retry-After` header value (default 2 s).",
    ),
}


# Subset for routes that don't require auth (e.g. healthcheck, version).
PUBLIC_RESPONSES: Dict[int | str, Dict[str, Any]] = {
    422: _err(422, "Request body or parameters failed validation."),
    503: _err(503, "Backend dependency (database, host net) is unavailable."),
}


# Top-level description rendered above the Swagger endpoint list.  Keep
# this product-focused and generic — no deployment-specific details.
DESCRIPTION = """
nova-ve is a network-emulation platform that runs labs of interconnected
virtual nodes (QEMU, Docker, Dynamips IOS) on a single Linux host.

This backend exposes the REST + WebSocket API the web UI consumes. The
same API is suitable for scripted lab provisioning, CI integration,
and external orchestration.

### Conventions

* **Envelope**: successful responses use `{code, status, message, data}`
  (see the `ApiResponse` schema). Errors use the same shape with a `fail`
  status and a non-2xx HTTP code (see `ErrorEnvelope`).
* **Lab paths** are URL-encoded relative paths under the configured labs
  directory. The same path may include forward slashes to nest labs in
  folders.
* **Authentication** is cookie-based. Call `POST /api/auth/login` first;
  subsequent calls reuse the `session` cookie. The "Authorize" button in
  Swagger UI is not used — log in via the endpoint and Swagger will pick
  up the cookie automatically.
* **Concurrency**: per-lab mutations serialise through an in-memory
  reader/writer lock. Writers that lose the race return `503` with a
  `Retry-After` header.

### Related documentation

* [Backend architecture](/docs/development/backend.md)
* [VMware deployment requirements](/docs/operations/vmware-deployment.md)
"""


CONTACT = {
    "name": "nova-ve",
    "url": "https://github.com/fahadysf/nova-ve",
}


LICENSE_INFO = {
    "name": "Apache 2.0",
    "url": "https://www.apache.org/licenses/LICENSE-2.0",
}


__all__ = [
    "ApiResponse",
    "CONTACT",
    "COMMON_RESPONSES",
    "DESCRIPTION",
    "ErrorEnvelope",
    "LICENSE_INFO",
    "OPENAPI_TAGS",
    "PUBLIC_RESPONSES",
]
