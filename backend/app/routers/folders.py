import time

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.schemas.folder import FolderCreateRequest, FolderRenameRequest
from app.schemas.user import UserRead
from app.services.folder_service import FolderService
from app.services.lab_service import LabService

router = APIRouter(prefix="/api/folders", tags=["folders"])


def _fmt_time(ts: float) -> str:
    return time.strftime("%d %b %Y %H:%M", time.localtime(ts))


async def _merge_db_labs(result: dict, db: AsyncSession, folder_path: str) -> dict:
    existing_paths = {lab["path"] for lab in result["labs"]}
    folder_prefix = folder_path.strip("/")
    lab_service = LabService(db)

    for lab in await lab_service.list_labs():
        relative_filename = lab.filename.strip("/")
        if folder_prefix:
            if "/" not in relative_filename:
                continue
            if relative_filename.rsplit("/", 1)[0] != folder_prefix:
                continue
        elif "/" in relative_filename:
            continue

        if lab.path in existing_paths:
            continue

        result["labs"].append(
            {
                "file": relative_filename.split("/")[-1],
                "path": lab.path,
                "umtime": int(time.time()),
                "mtime": _fmt_time(time.time()),
                "spy": -1,
                "lock": False,
                "shared": 0,
            }
        )
    return result


@router.get("/")
async def list_root(
    current_user: UserRead = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        result = FolderService.list_folder("")
        result = await _merge_db_labs(result, db, "")
    except ValueError as e:
        return {
            "code": 400,
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
        result = FolderService.list_folder(folder_path)
        result = await _merge_db_labs(result, db, folder_path)
    except ValueError as e:
        return {
            "code": 400,
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
        FolderService.create_folder_path(request.path, request.name)
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

    target_path = "/".join(
        part for part in [request.path.strip("/"), (request.name or "").strip("/")] if part
    )
    return {
        "code": 200,
        "status": "success",
        "message": "Folder created successfully.",
        "data": {"path": f"/{target_path}" if target_path else "/"},
    }


@router.put("/{folder_path:path}")
async def rename_folder(
    folder_path: str,
    request: FolderRenameRequest,
    current_user: UserRead = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        FolderService.rename_folder(folder_path, request.path)
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

    old_prefix = folder_path.strip("/")
    new_prefix = request.path.strip("/")
    lab_service = LabService(db)
    for lab in await lab_service.list_labs():
        if lab.filename == old_prefix or lab.filename.startswith(f"{old_prefix}/"):
            suffix = lab.filename[len(old_prefix) :].lstrip("/")
            new_filename = "/".join(part for part in [new_prefix, suffix] if part)
            lab.filename = new_filename
            lab.path = f"/{new_filename}"

    await db.commit()

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
    try:
        FolderService.delete_folder(folder_path)
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

    folder_prefix = folder_path.strip("/")
    lab_service = LabService(db)
    for lab in await lab_service.list_labs():
        if lab.filename == folder_prefix or lab.filename.startswith(f"{folder_prefix}/"):
            await db.delete(lab)

    await db.commit()

    return {
        "code": 200,
        "status": "success",
        "message": "Folder deleted successfully.",
    }
