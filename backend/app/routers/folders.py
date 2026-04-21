from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.dependencies import get_current_user
from app.schemas.user import UserRead
from app.schemas.folder import FolderListResponse, FolderItem, LabListItem
import os
import time
from pathlib import Path
from app.config import get_settings

router = APIRouter(prefix="/api/folders", tags=["folders"])


def _fmt_time(ts: float) -> str:
    return time.strftime("%d %b %Y %H:%M", time.localtime(ts))


@router.get("/")
async def list_root(
    current_user: UserRead = Depends(get_current_user),
):
    settings = get_settings()
    base = settings.LABS_DIR

    folders = [
        FolderItem(name="Running", path="/Running"),
        FolderItem(name="Shared", path="/Shared"),
        FolderItem(name="Users", path="/Users"),
    ]

    labs = []
    if base.exists():
        for f in sorted(base.glob("*.json")):
            st = f.stat()
            labs.append(LabListItem(
                file=f.name,
                path=f"/{f.name}",
                umtime=int(st.st_mtime),
                mtime=_fmt_time(st.st_mtime),
            ))

    return {
        "code": 200,
        "status": "success",
        "message": "Successfully listed path (60007).",
        "data": FolderListResponse(folders=folders, labs=labs).model_dump(),
    }
