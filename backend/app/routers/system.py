from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.dependencies import get_current_admin, get_current_user
from app.openapi import COMMON_RESPONSES
from app.schemas.user import UserRead
from app.services.cloud_inventory_service import list_nat_clouds
from app.services.bridge_cloud_service import list_bridge_clouds
from app.services import audit_service, iourc_service, qemu_versions
from app.services.node_runtime_service import NodeRuntimeService
import ipaddress
import json
import logging
import platform
import time
from pathlib import Path

import psutil

from pydantic import BaseModel

from app.config import CONSOLE_TERMINAL_FONT_OPTIONS, EDITABLE_SETTING_KEYS, get_settings

router = APIRouter(prefix="/api", tags=["system"], responses=COMMON_RESPONSES)

_logger = logging.getLogger("nova-ve.system")

# Editable settings subset (#265). Values persist to
# BASE_DATA_DIR/system-settings.json and take effect on backend restart —
# get_settings() applies them at boot, and the GET below reports which keys
# carry a persisted override so the UI can flag "restart required" after an
# edit. The tuple lives in config.py (single source of truth).


def _settings_overrides_path():
    return get_settings().BASE_DATA_DIR / "system-settings.json"


def _load_settings_overrides() -> dict:
    try:
        raw = json.loads(_settings_overrides_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: raw[k] for k in EDITABLE_SETTING_KEYS if k in raw} if isinstance(raw, dict) else {}


def _validate_override(key: str, value) -> object:
    if key == "DISCOVERY_CADENCE_SECONDS":
        ivalue = int(value)
        if not 5 <= ivalue <= 300:
            raise ValueError("DISCOVERY_CADENCE_SECONDS must be between 5 and 300 seconds.")
        return ivalue
    if key == "NAT_CLOUD_POOL":
        network = ipaddress.ip_network(str(value).strip(), strict=True)
        if isinstance(network, ipaddress.IPv6Network):
            raise ValueError("NAT_CLOUD_POOL must be IPv4.")
        if network.prefixlen > 24:
            raise ValueError("NAT_CLOUD_POOL must be at least large enough to allocate /24 networks.")
        return str(network)
    if key == "SESSION_MAX_AGE":
        ivalue = int(value)
        if ivalue < 300:
            raise ValueError("SESSION_MAX_AGE must be at least 300 seconds.")
        return ivalue
    if key == "EXTERNAL_CONSOLE_HOST":
        # External console launch (#269): the host advertised verbatim in
        # telnet://, vnc:// and rdp:// URLs. Must be a bare hostname or IPv4
        # address — no scheme, port, path, or userinfo — so the emitted
        # host:port URLs stay unambiguous. An empty value persists as an
        # explicit "feature disabled" override.
        text_value = str(value).strip()
        if not text_value:
            return ""
        if any(char in text_value for char in ("/", ":", "@", " ")) or not text_value.replace(".", "").replace("-", "").isalnum():
            raise ValueError(
                "EXTERNAL_CONSOLE_HOST must be a bare hostname or IPv4 address "
                "(no scheme, port, path, or credentials)."
            )
        return text_value
    if key == "GUACAMOLE_TERMINAL_FONT_NAME":
        # #283: default terminal font for in-browser console sessions.
        # Free-form (fontconfig family name resolved inside guacd); the
        # shipped options list only drives the UI dropdown. Empty = use
        # guacd's own default.
        font_value = str(value).strip()
        if len(font_value) > 64 or any(ord(char) < 32 for char in font_value):
            raise ValueError(
                "GUACAMOLE_TERMINAL_FONT_NAME must be a font family name "
                "(64 characters max, no control characters)."
            )
        return font_value
    raise ValueError(f"Unsupported setting: {key}")


@router.get("/health")
async def healthcheck(
    db: AsyncSession = Depends(get_db),
):
    await db.execute(text("SELECT 1"))
    return {
        "code": 200,
        "status": "success",
        "message": "Healthcheck passed.",
        "data": {"database": "ok"},
    }


@router.get("/status")
async def get_status(
    current_user: UserRead = Depends(get_current_user),
):
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage("/")
    runtime_counts = NodeRuntimeService().runtime_counts()
    settings = get_settings()
    return {
        "code": 200,
        "status": "success",
        "message": "Fetched system status (60001).",
        "data": {
            "version": "0.1.0-alpha",
            # #278: the real distro QEMU version (was a hardcoded 8.0.0).
            "qemu_version": qemu_versions.default_version(),
            "uksm": "unsupported",
            "ksm": "unsupported",
            "cpulimit": "enabled",
            "cpu": str(psutil.cpu_count(logical=False)),
            "vCPU": str(psutil.cpu_count(logical=True)),
            "disk": f"{disk.used / (1024**3):.4f}",
            "diskavailable": f"{disk.free / (1024**3):.4f}",
            "mindisk": 10,
            "memtotal": str(mem.total // 1024),
            "cached": 0,
            "mem": f"{mem.percent:.4f}",
            "swap": f"{swap.percent:.4f}",
            "swapavailable": str(swap.free // 1024),
            "iol": runtime_counts["iol"],
            "dynamips": runtime_counts["dynamips"],
            "qemu": runtime_counts["qemu"],
            "docker": runtime_counts["docker"],
            "vpcs": runtime_counts["vpcs"],
            # #265 — live host facts for the status dashboard.
            "cpu_percent": psutil.cpu_percent(interval=None),
            "kernel": platform.release(),
            "hostname": platform.node(),
            "uptime_seconds": int(time.time() - psutil.boot_time()),
            "mem_used_gb": round((mem.total - mem.available) / 1024**3, 2),
            "mem_total_gb": round(mem.total / 1024**3, 2),
            "disk_used_gb": round(disk.used / 1024**3, 2),
            "disk_total_gb": round(disk.total / 1024**3, 2),
            "server_id": get_settings().SECRET_KEY[:8],
            "labs_dir": str(settings.LABS_DIR),
            "images_dir": str(settings.IMAGES_DIR),
        },
    }


@router.get("/system/qemu-versions")
async def get_qemu_versions(
    current_user: UserRead = Depends(get_current_user),
):
    """Installed side-by-side QEMU versions (#278).

    Feeds the node-form/template emulator dropdowns and boot-time
    validation hints. ``default`` is the distribution QEMU that every node
    boots on when ``extras.qemu_version`` is empty; ``versions`` lists the
    optional ``nova-ve-qemu-*`` packages installed under
    ``QEMU_VERSIONS_ROOT`` (``/opt/nova-ve/qemu/<version>/``).
    """
    return {
        "code": 200,
        "status": "success",
        "message": "QEMU versions successfully fetched.",
        "data": qemu_versions.catalog(),
    }


@router.get("/system/settings")
async def get_settings_route(
    current_user: UserRead = Depends(get_current_user),
):
    """Effective operational settings (#265): live config values plus any
    persisted overrides (which apply on backend restart)."""
    settings = get_settings()
    overrides = _load_settings_overrides()
    return {
        "code": 200,
        "status": "success",
        "message": "System settings successfully fetched (60078).",
        "data": {
            "auth": {
                "session_max_age": overrides.get("SESSION_MAX_AGE", settings.SESSION_MAX_AGE),
                "session_max_age_effective": settings.SESSION_MAX_AGE,
                "session_cookie": settings.SESSION_COOKIE_NAME,
                "cookie_secure": settings.COOKIE_SECURE,
                "access_token_expire_minutes": settings.ACCESS_TOKEN_EXPIRE_MINUTES,
            },
            "networking": {
                "nat_cloud_pool": overrides.get("NAT_CLOUD_POOL", settings.NAT_CLOUD_POOL),
                "nat_cloud_pool_effective": settings.NAT_CLOUD_POOL,
                "docker_host": settings.DOCKER_HOST,
                # External console launch (#269): empty disables the feature;
                # when set it is advertised verbatim in telnet://, vnc:// and
                # rdp:// URLs and newly started console listeners bind the
                # wildcard address (see config.py for the exposure notes).
                "external_console_host": overrides.get(
                    "EXTERNAL_CONSOLE_HOST", getattr(settings, "EXTERNAL_CONSOLE_HOST", "")
                ),
                "external_console_host_effective": getattr(
                    settings, "EXTERNAL_CONSOLE_HOST", ""
                ),
            },
            "reconciliation": {
                "discovery_cadence_seconds": overrides.get(
                    "DISCOVERY_CADENCE_SECONDS", settings.DISCOVERY_CADENCE_SECONDS
                ),
                "discovery_cadence_seconds_effective": settings.DISCOVERY_CADENCE_SECONDS,
            },
            "console": {
                "guacamole_public_path": settings.GUACAMOLE_PUBLIC_PATH,
                "terminal_font_name": overrides.get(
                    "GUACAMOLE_TERMINAL_FONT_NAME", settings.GUACAMOLE_TERMINAL_FONT_NAME
                ),
                "terminal_font_name_effective": settings.GUACAMOLE_TERMINAL_FONT_NAME,
                # Font families shipped with nova-ve (#283); the dropdown's
                # option list. Free-form values stay valid for custom fonts.
                "terminal_font_options": list(CONSOLE_TERMINAL_FONT_OPTIONS),
                "terminal_font_size": settings.GUACAMOLE_TERMINAL_FONT_SIZE,
            },
            "paths": {
                "labs_dir": str(settings.LABS_DIR),
                "images_dir": str(settings.IMAGES_DIR),
                "templates_dir": str(settings.TEMPLATES_DIR),
                "user_templates_dir": str(settings.USER_TEMPLATES_DIR),
            },
            "overrides": sorted(overrides.keys()),
            "restart_required": bool(overrides),
        },
    }


class SettingsUpdateBody(BaseModel):
    DISCOVERY_CADENCE_SECONDS: int | None = None
    NAT_CLOUD_POOL: str | None = None
    SESSION_MAX_AGE: int | None = None
    EXTERNAL_CONSOLE_HOST: str | None = None
    GUACAMOLE_TERMINAL_FONT_NAME: str | None = None


@router.put("/system/settings")
async def put_settings_route(
    body: SettingsUpdateBody,
    current_user: UserRead = Depends(get_current_admin),
):
    """Persist editable settings (#265). Values apply on backend restart."""
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        return JSONResponse(
            status_code=422,
            content={"code": 422, "status": "fail", "message": "No editable settings supplied."},
        )

    validated: dict = {}
    try:
        for key, value in updates.items():
            if value is not None:
                validated[key] = _validate_override(key, value)
    except (ValueError, TypeError) as exc:
        return JSONResponse(
            status_code=422,
            content={"code": 422, "status": "fail", "message": str(exc)},
        )

    previous = _load_settings_overrides()
    overrides = dict(previous)
    overrides.update(validated)
    path = _settings_overrides_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(overrides, indent=1, sort_keys=True))
    except OSError as exc:
        return JSONResponse(
            status_code=500,
            content={"code": 500, "status": "fail", "message": f"Failed to persist settings: {exc}"},
        )

    # #300: which settings took a new value. Saving the values already in
    # force is not an event worth a row.
    changes = {
        key: {"from": previous.get(key), "to": value}
        for key, value in validated.items()
        if previous.get(key) != value
    }
    if changes:
        await audit_service.record(
            "system.settings",
            actor=current_user.username,
            target_type="system",
            target_id="settings",
            target_name="System settings",
            detail={"changes": changes, "restart_required": True},
        )
    return {
        "code": 200,
        "status": "success",
        "message": "Settings persisted. Restart the backend to apply.",
        "data": {"overrides": sorted(overrides.keys()), "restart_required": True},
    }


@router.get("/system/iourc")
async def get_iourc_status(
    current_user: UserRead = Depends(get_current_user),
):
    return {
        "code": 200,
        "status": "success",
        "message": "IOURC license status fetched.",
        "data": iourc_service.status().as_dict(),
    }


@router.post("/system/iourc")
async def upload_iourc(
    file: UploadFile = File(...),
    current_user: UserRead = Depends(get_current_user),
):
    content = await file.read(iourc_service.IOURC_MAX_BYTES + 1)
    try:
        saved = iourc_service.store_uploaded_iourc(content)
    except iourc_service.IourcError as exc:
        return JSONResponse(
            status_code=400,
            content={
                "code": 400,
                "status": "fail",
                "message": str(exc),
            },
        )
    # #300: that the license was replaced is the fact worth keeping. Its
    # contents are a secret and are never written to the trail.
    await audit_service.record(
        "system.iourc",
        actor=current_user.username,
        target_type="system",
        target_id="iourc",
        target_name="IOL license",
    )
    return {
        "code": 200,
        "status": "success",
        "message": "IOURC license uploaded.",
        "data": saved.as_dict(),
    }


@router.get("/system/bridge-clouds")
async def get_bridge_clouds(
    current_user: UserRead = Depends(get_current_user),
):
    """Enumerate host-owned Bridge-Cloud bridges (``br-eth*``).

    Used by the Bridge-Cloud network-config modal in the frontend to
    populate the host-bridge dropdown.  Auth-gated (mirrors
    ``/api/status`` / ``/api/poll``).  Results are cached server-side for
    5 s to dampen UI poll cost.
    """
    return {
        "code": 200,
        "status": "success",
        "data": await list_bridge_clouds(),
    }


@router.get("/system/clouds")
async def get_clouds(
    current_user: UserRead = Depends(get_current_admin),
):
    """Enumerate reusable host cloud resources for network creation."""
    return {
        "code": 200,
        "status": "success",
        "data": {
            "nat_clouds": list_nat_clouds(),
            "bridge_clouds": await list_bridge_clouds(),
            "shared_bridges": [],
        },
    }


@router.get("/poll")
async def poll(
    current_user: UserRead = Depends(get_current_user),
):
    disk = psutil.disk_usage("/")
    return {
        "code": 200,
        "status": "success",
        "data": {
            "diskavailable": f"{disk.free / (1024**3):.4f}",
            "disk": f"{disk.used / (1024**3):.4f}",
            "mindisk": 10,
        },
    }


# ----------------------------------------------------------------------
# Log viewer (#265): per-node runtime logs, pattern-filtered tail.
# ----------------------------------------------------------------------
def _docker_log_entry(record_file, record, lab_id: str, node_id) -> dict:
    """Synthetic listing entry for a docker node's daemon-side logs."""
    return {
        "name": record_file.name.replace(".json", ""),
        "label": str(record.get("name") or record_file.stem),
        "kind": "docker",
        "lab_id": lab_id,
        "node_id": node_id,
        "path": f"docker://{lab_id}/{node_id}",
        "size": 0,
        "mtime": int(time.time()),
    }


def _collect_log_files() -> list[dict]:
    """Every node runtime log on disk or in the docker daemon.

    Runtime records live in TMP_DIR/node-runtime/*.json. qemu/iol/dynamips
    records carry ``stdout_log`` (addressable by file path); docker records
    have no stdout file — they are addressable via the synthetic
    ``docker://{lab_id}/{node_id}`` path and read through the runtime
    service's docker-logs adapter.
    """
    settings = get_settings()
    records_dir = settings.TMP_DIR / "node-runtime"
    entries: list[dict] = []
    if not records_dir.exists():
        return entries
    for record_file in sorted(records_dir.glob("*.json")):
        try:
            record = json.loads(record_file.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        lab_id = str(record.get("lab_id") or "")
        node_id = record.get("node_id")
        kind = str(record.get("kind") or "")
        log_path_raw = record.get("stdout_log")
        if not log_path_raw:
            if kind == "docker" and lab_id and node_id is not None:
                entries.append(_docker_log_entry(record_file, record, lab_id, node_id))
            continue
        log_path = Path(str(log_path_raw))
        try:
            stat = log_path.stat()
        except OSError:
            # Docker records reference a stdout file that never materializes
            # (container logs live in the docker daemon) — fall through to
            # the synthetic docker:// path so they stay visible.
            if kind == "docker" and lab_id and node_id is not None:
                entries.append(_docker_log_entry(record_file, record, lab_id, node_id))
            continue
        # Path safety: only surface files under TMP_DIR.
        try:
            resolved = log_path.resolve()
            if settings.TMP_DIR.resolve() not in resolved.parents:
                continue
        except OSError:
            continue
        entries.append(
            {
                "name": record_file.name.replace(".json", ""),
                "label": str(record.get("name") or record_file.stem),
                "kind": kind,
                "lab_id": lab_id,
                "node_id": node_id,
                "path": str(resolved),
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
            }
        )
    return entries


@router.get("/system/logs")
async def list_log_files(
    current_user: UserRead = Depends(get_current_admin),
):
    return {
        "code": 200,
        "status": "success",
        "message": "Log files listed.",
        "data": {"logs": _collect_log_files()},
    }


@router.get("/system/logs/tail")
async def tail_log_file(
    path: str,
    lines: int = 200,
    pattern: str = "",
    current_user: UserRead = Depends(get_current_admin),
):
    """Tail a runtime log, optionally filtering by substring.

    File-backed logs must match one of the listed entries verbatim
    (path-safety allowlist); ``docker://`` synthetic paths read through the
    runtime service's docker-logs adapter.
    """
    if lines < 1 or lines > 2000:
        return JSONResponse(
            status_code=422,
            content={"code": 422, "status": "fail", "message": "lines must be 1..2000."},
        )

    raw_path = str(path)
    if raw_path.startswith("docker://"):
        allowed = {entry["path"]: entry for entry in _collect_log_files()}
        target = allowed.get(raw_path)
        if target is None:
            return JSONResponse(
                status_code=404,
                content={"code": 404, "status": "fail", "message": "Unknown log file."},
            )
        try:
            from app.services.node_runtime_service import NodeRuntimeService

            content = NodeRuntimeService().read_logs(
                target["lab_id"], int(target["node_id"] or 0), tail=lines
            )
        except (ValueError, TypeError):
            return JSONResponse(
                status_code=422,
                content={"code": 422, "status": "fail", "message": "Malformed docker log path."},
            )
        except Exception as exc:
            return JSONResponse(
                status_code=500,
                content={"code": 500, "status": "fail", "message": f"Docker logs read failed: {exc}"},
            )
        collected = content.splitlines(keepends=True)
        needle = pattern.strip().lower()
        if needle:
            collected = [line for line in collected if needle in line.lower()]
        return {
            "code": 200,
            "status": "success",
            "message": "Log tail fetched.",
            "data": {
                "log": target,
                "lines": len(collected),
                "pattern": needle,
                "content": "".join(collected),
            },
        }

    allowed = {entry["path"]: entry for entry in _collect_log_files()}
    target = allowed.get(str(Path(path).resolve()))
    if target is None:
        return JSONResponse(
            status_code=404,
            content={"code": 404, "status": "fail", "message": "Unknown log file."},
        )

    try:
        with open(target["path"], "r", encoding="utf-8", errors="ignore") as handle:
            collected = handle.readlines()[-lines:]
    except OSError as exc:
        return JSONResponse(
            status_code=500,
            content={"code": 500, "status": "fail", "message": f"Read failed: {exc}"},
        )

    needle = pattern.strip().lower()
    if needle:
        collected = [line for line in collected if needle in line.lower()]

    return {
        "code": 200,
        "status": "success",
        "message": "Log tail fetched.",
        "data": {
            "log": target,
            "lines": len(collected),
            "pattern": needle,
            "content": "".join(collected),
        },
    }


@router.get("/system/logs/activity")
async def list_activity_log(
    q: str = "",
    category: str = "",
    action: str = "",
    actor: str = "",
    from_ts: int | None = Query(default=None, alias="from"),
    to_ts: int | None = Query(default=None, alias="to"),
    limit: int = Query(default=audit_service.DEFAULT_PAGE_SIZE, ge=1, le=audit_service.MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    current_user: UserRead = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """User activity trail (#297) — who did what, from where, in which lab.

    Admin only. ``q`` is free text over actor, target, lab, source address
    and detail; ``category``/``action``/``actor`` narrow it; ``from``/``to``
    are epoch-second bounds; ``limit``/``offset`` page through the result.
    """
    page = await audit_service.list_entries(
        db,
        q=q,
        category=category,
        action=action,
        actor=actor,
        since=from_ts,
        until=to_ts,
        limit=limit,
        offset=offset,
    )
    return {
        "code": 200,
        "status": "success",
        "message": "Activity log loaded.",
        "data": page,
    }
