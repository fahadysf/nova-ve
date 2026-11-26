import asyncio
import logging
import time

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.openapi import COMMON_RESPONSES
from app.schemas.folder import FolderCreateRequest, FolderRenameRequest
from app.schemas.user import UserRead
from app.services.async_utils import notify_after_mutation, wait_for_mutation
from app.services.cloud_inventory_service import SharedCloudReferenceError
from app.services.shared_network_service import assert_no_external_members_for_scope
from app.services.folder_service import FolderService, _normalize_folder_path, folder_mutation_lock
from app.services import audit_service
from app.services.lab_service import LabService

logger = logging.getLogger("nova-ve.folders")
from app.services.lab_service import LabService

router = APIRouter(
    prefix="/api/folders",
    tags=["folders"],
    responses=COMMON_RESPONSES,
)


def _fmt_time(ts: float) -> str:
    return time.strftime("%d %b %Y %H:%M", time.localtime(ts))


def _user_root(current_user: UserRead) -> str:
    if getattr(current_user, "role", "admin") == "admin":
        return ""
    return getattr(current_user, "folder", "/").strip().replace("\\", "/").strip("/")


def _scoped_folder_path(current_user: UserRead, raw_path: str, treat_as_absolute: bool) -> str:
    normalized = raw_path.strip().replace("\\", "/").strip("/")
    # #285: /Shared is a real read-write space for every authenticated user,
    # resolved at the labs root — never prefixed into a scoped user's root.
    if normalized == "Shared" or normalized.startswith("Shared/"):
        return normalized
    root = _user_root(current_user)
    if not root:
        return normalized

    if not normalized:
        return root

    if normalized == root or normalized.startswith(f"{root}/"):
        return normalized

    if treat_as_absolute or raw_path.strip().startswith("/"):
        raise PermissionError("Access denied.")

    return f"{root}/{normalized}"


SHARED_OWNER = "*"  # rendered as "All users" by the UI (#322)
SYSTEM_OWNER = "system"
_SYSTEM_FOLDERS = {"Running", "Users"}


def folder_owner(path: str) -> str:
    """Owner of a folder path: top-level ``/<user>`` folders belong to that
    user (children inherit), ``/Shared`` belongs to every user, and the
    virtual ``Running``/``Users`` roots belong to the system."""
    top = path.strip("/").split("/", 1)[0]
    if not top:
        return SYSTEM_OWNER
    if top == "Shared":
        return SHARED_OWNER
    if top in _SYSTEM_FOLDERS:
        return SYSTEM_OWNER
    return top


def annotate_owners(result: dict, lab_owners: dict[str, str] | None = None) -> dict:
    """Add an additive ``owner`` field to every folder and lab entry."""
    lab_owners = lab_owners or {}
    for folder in result.get("folders", []):
        folder.setdefault("owner", folder_owner(folder["path"]))
    for lab in result.get("labs", []):
        lab.setdefault("owner", lab_owners.get(lab["path"]) or folder_owner(lab["path"].rsplit("/", 1)[0]))
    return result


async def _merge_db_labs(
    result: dict,
    db: AsyncSession,
    folder_path: str,
    recursive: bool = False,
    lab_ids: dict[str, str] | None = None,
) -> dict:
    """Union DB lab rows into a file-scan listing.

    ``lab_ids`` (optional) collects the row id of every in-scope lab that
    has a backing file, keyed by path — the id→path map the running
    annotation needs (#365), taken from rows this pass already loads.
    """
    existing_paths = {lab["path"] for lab in result["labs"]}
    lab_owners: dict[str, str] = {}
    folder_prefix = folder_path.strip("/")
    lab_service = LabService(db)

    for lab in await lab_service.list_labs():
        relative_filename = lab.filename.strip("/")
        # #284: a DB row whose backing file was removed out-of-band is a
        # ghost — never list it (the file scan below is authoritative for
        # what actually exists). The startup reconciliation drops the row.
        if not LabService.file_exists(lab.filename):
            logger.warning(
                "labs listing: skipping ghost lab row without backing file (filename=%s)",
                lab.filename,
            )
            continue
        if folder_prefix:
            if recursive:
                if not (relative_filename == folder_prefix or relative_filename.startswith(f"{folder_prefix}/")):
                    continue
            else:
                if "/" not in relative_filename:
                    continue
                if relative_filename.rsplit("/", 1)[0] != folder_prefix:
                    continue
        elif not recursive and "/" in relative_filename:
            continue

        lab_owners[lab.path] = lab.owner
        row_id = getattr(lab, "id", None)
        if lab_ids is not None and row_id:
            lab_ids[lab.path] = str(row_id)
        if lab.path in existing_paths:
            continue

        result["labs"].append(
            {
                "owner": lab.owner,
                "file": relative_filename.split("/")[-1],
                "path": lab.path,
                "umtime": int(time.time()),
                "mtime": _fmt_time(time.time()),
                "spy": -1,
                "lock": False,
                "shared": 0,
            }
        )
    return annotate_owners(result, lab_owners)


async def _annotate_running(result: dict, lab_ids: dict[str, str]) -> None:
    """Attach ``nodes_running`` to the listed labs (#365).

    Liveness checks block on ``/proc`` reads and docker/hypervisor queries,
    so they run off the event loop (#277).
    """
    await asyncio.to_thread(FolderService.annotate_running, result["labs"], lab_ids)


@router.get("/")
async def list_root(
    current_user: UserRead = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        scoped_root = _scoped_folder_path(current_user, "", treat_as_absolute=False)
        result = FolderService.list_folder(scoped_root, recursive=True, at_user_root=True)
        lab_ids: dict[str, str] = {}
        result = await _merge_db_labs(result, db, scoped_root, recursive=True, lab_ids=lab_ids)
        await _annotate_running(result, lab_ids)
    except ValueError as e:
        return {
            "code": 400,
            "status": "fail",
            "message": str(e),
        }
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    return {
        "code": 200,
        "status": "success",
        "message": "Successfully listed path (60007).",
        "data": result,
    }


@router.get("/{folder_path:path}")
async def list_folder(
    folder_path: str = "",
    current_user: UserRead = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        scoped_path = _scoped_folder_path(current_user, folder_path, treat_as_absolute=True)
        result = FolderService.list_folder(scoped_path)
        lab_ids: dict[str, str] = {}
        result = await _merge_db_labs(result, db, scoped_path, lab_ids=lab_ids)
        await _annotate_running(result, lab_ids)
    except ValueError as e:
        return {
            "code": 400,
            "status": "fail",
            "message": str(e),
        }
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    return {
        "code": 200,
        "status": "success",
        "message": "Successfully listed path (60007).",
        "data": result,
    }


@router.post("/")
async def create_folder(
    request: FolderCreateRequest,
    current_user: UserRead = Depends(get_current_user),
):
    try:
        scoped_path = _scoped_folder_path(current_user, request.path, treat_as_absolute=False)
        FolderService.create_folder_path(scoped_path, request.name)
    except FileExistsError as e:
        return {
            "code": 400,
            "status": "fail",
            "message": str(e),
        }
    except ValueError as e:
        return {
            "code": 400,
            "status": "fail",
            "message": str(e),
        }
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    target_path = "/".join(
        part for part in [scoped_path.strip("/"), (request.name or "").strip("/")] if part
    )
    created_path = f"/{target_path}" if target_path else "/"
    await audit_service.record(
        "folder.create",
        actor=current_user.username,
        target_type="folder",
        target_id=created_path,
        target_name=request.name or created_path,
    )
    return {
        "code": 200,
        "status": "success",
        "message": "Folder created successfully.",
        "data": {"path": created_path},
    }


@router.put("/{folder_path:path}")
async def rename_folder(
    folder_path: str,
    request: FolderRenameRequest,
    current_user: UserRead = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    async def rename_and_commit(old_prefix: str, new_prefix: str) -> None:
        FolderService.rename_folder(old_prefix, new_prefix)
        for lab in await LabService(db).list_labs():
            if lab.filename == old_prefix or lab.filename.startswith(f"{old_prefix}/"):
                suffix = lab.filename[len(old_prefix) :].lstrip("/")
                lab.filename = "/".join(part for part in [new_prefix, suffix] if part)
                lab.path = f"/{lab.filename}"
        await db.commit()

    try:
        old_prefix = _normalize_folder_path(_scoped_folder_path(current_user, folder_path, treat_as_absolute=True))
        new_prefix = _normalize_folder_path(_scoped_folder_path(current_user, request.path, treat_as_absolute=False))
        async with folder_mutation_lock():
            _, cancelled = await wait_for_mutation(asyncio.create_task(rename_and_commit(old_prefix, new_prefix)))
    except FileNotFoundError as e:
        return {
            "code": 404,
            "status": "fail",
            "message": str(e),
        }
    except FileExistsError as e:
        return {
            "code": 400,
            "status": "fail",
            "message": str(e),
        }
    except ValueError as e:
        return {
            "code": 400,
            "status": "fail",
            "message": str(e),
        }
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    await notify_after_mutation(
        audit_service.record(
            "folder.rename",
            actor=current_user.username,
            target_type="folder",
            target_id=f"/{old_prefix}",
            detail={"from": f"/{old_prefix}", "to": f"/{new_prefix}" if new_prefix else "/"},
        ),
        cancelled,
    )
    return {
        "code": 200,
        "status": "success",
        "message": "Folder renamed successfully.",
        "data": {"old_path": f"/{old_prefix}", "new_path": f"/{new_prefix}" if new_prefix else "/"},
    }


@router.delete("/{folder_path:path}")
async def delete_folder(
    folder_path: str,
    current_user: UserRead = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    async def delete_and_commit(folder_prefix: str) -> None:
        await asyncio.to_thread(assert_no_external_members_for_scope, folder_prefix)
        await asyncio.to_thread(FolderService.delete_folder, folder_prefix)
        for lab in await LabService(db).list_labs():
            if lab.filename == folder_prefix or lab.filename.startswith(f"{folder_prefix}/"):
                await db.delete(lab)
        await db.commit()

    try:
        folder_prefix = _normalize_folder_path(_scoped_folder_path(current_user, folder_path, treat_as_absolute=True))
        async with folder_mutation_lock():
            _, cancelled = await wait_for_mutation(asyncio.create_task(delete_and_commit(folder_prefix)))
    except SharedCloudReferenceError as e:
        return {
            "code": 409,
            "status": "fail",
            "message": e.message,
            "data": {"references": e.references},
        }
    except FileNotFoundError as e:
        return {
            "code": 404,
            "status": "fail",
            "message": str(e),
        }
    except OSError as e:
        return {
            "code": 400,
            "status": "fail",
            "message": str(e),
        }
    except ValueError as e:
        return {
            "code": 400,
            "status": "fail",
            "message": str(e),
        }
    except PermissionError as e:
        return {
            "code": 403,
            "status": "fail",
            "message": str(e),
        }

    await notify_after_mutation(
        audit_service.record(
            "folder.delete",
            actor=current_user.username,
            target_type="folder",
            target_id=f"/{folder_prefix}",
        ),
        cancelled,
    )
    return {
        "code": 200,
        "status": "success",
        "message": "Folder deleted successfully.",
    }
