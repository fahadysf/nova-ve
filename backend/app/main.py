# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from app.routers import auth, folders, labs, links, listing, networks, system, users, ws
from app.database import engine
from app.config import get_settings
from app.services.lab_lock import LabLockTimeout
from app.services.host_net import HostNetInstanceIdMissing, get_instance_id


def lab_lock_timeout_handler(request: Request, exc: LabLockTimeout) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "2"},
        content={
            "code": 503,
            "status": "fail",
            "message": str(exc) or "Lab is busy; retry in 2s",
        },
    )

settings = get_settings()
app = FastAPI(title="nova-ve", version="0.1.0-alpha")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:4173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(folders.router)
# v2 per-resource routers must be included BEFORE labs.router so they take
# precedence on shared verbs (POST /networks, GET /networks, DELETE /networks).
app.include_router(links.router)
app.include_router(networks.router)
app.include_router(labs.router)
app.include_router(listing.router)
app.include_router(system.router)
app.include_router(users.router)
app.include_router(ws.router)

app.add_exception_handler(LabLockTimeout, lab_lock_timeout_handler)


@app.on_event("startup")
async def startup():
    import logging
    _logger = logging.getLogger("nova-ve")
    try:
        instance_id = get_instance_id()
        _logger.info("nova-ve instance ID: %s", instance_id)
    except HostNetInstanceIdMissing as exc:
        _logger.critical(
            "FATAL: instance ID not provisioned — deploy/scripts/provision-ubuntu-2604.sh did not run. "
            "Cannot derive collision-resistant bridge names. %s",
            exc,
        )
        raise SystemExit(1) from exc

    from sqlalchemy import text
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_schema='public' AND table_name='users')")
        )
        has_users = result.scalar()
        if not has_users:
            import logging
            logger = logging.getLogger("nova-ve")
            logger.warning(
                "Database tables not found. Run: cd backend && alembic upgrade head && python -m scripts.seed"
            )

    from app.services.node_runtime_service import NodeRuntimeService
    NodeRuntimeService.start_heartbeat()

    # US-206: backend-startup orphan sweep.  Scan the host for nove*/nve* kernel
    # objects that belong to labs no longer on disk (or that survived a crash).
    # Best-effort — never blocks startup, never raises.
    try:
        from app.services import host_net as _host_net
        from app.services.lab_service import LabService as _LabService

        _known_lab_ids: set[str] = set()
        _labs_dir = settings.LABS_DIR
        if _labs_dir.exists():
            for _lab_file in _labs_dir.rglob("*.json"):
                try:
                    import json as _json
                    _raw = _json.loads(_lab_file.read_text())
                    _lid = str(_raw.get("id", "")).strip()
                    if _lid:
                        _known_lab_ids.add(_lid)
                except Exception:
                    pass

        _removed_bridges = _host_net.sweep_orphan_bridges(_known_lab_ids)
        _removed_ifaces = _host_net.sweep_orphan_ifaces(_known_lab_ids)
        if _removed_bridges or _removed_ifaces:
            _logger.warning(
                "startup orphan-sweep: removed %d bridges %s and %d ifaces %s",
                len(_removed_bridges), _removed_bridges,
                len(_removed_ifaces), _removed_ifaces,
            )
        else:
            _logger.info("startup orphan-sweep: no orphans found")
    except Exception:
        _logger.exception("startup orphan-sweep failed (non-fatal)")


@app.on_event("shutdown")
async def shutdown():
    await engine.dispose()


@app.get("/VERSION")
async def version():
    return "0.1.0-alpha"


@app.get("/local_version")
async def local_version():
    return "0.1.0-alpha"


@app.get("/online_version")
async def online_version():
    return "0.1.0-alpha"
