from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.dependencies import get_current_user
from app.schemas.user import UserRead
from app.schemas.folder import FolderListResponse, FolderItem, LabListItem
from app.models.lab import LabMeta
import time
from pathlib import Path
from app.config import get_settings

router = APIRouter(prefix="/api/folders", tags=["folders"])


def _fmt_time(ts: float) -> str:
    return time.strftime("%d %b %Y %H:%M", time.localtime(ts))


@router.get("/")
async def list_root(
    current_user: UserRead = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    folders = [
        FolderItem(name="Running", path="/Running"),
        FolderItem(name="Shared", path="/Shared"),
        FolderItem(name="Users", path="/Users"),
    ]

    # List labs from DB (created via API)
    labs = []
    result = await db.execute(select(LabMeta))
    db_labs = result.scalars().all()

    for lab in db_labs:
        labs.append(LabListItem(
            file=lab.filename,
            path=f"/{lab.filename}",
            umtime=int(time.time()),
            mtime=_fmt_time(time.time()),
        ))

    # Also scan filesystem for labs not yet in DB (legacy/seeded files)
    settings = get_settings()
    base = settings.LABS_DIR
    if base.exists():
        db_filenames = {lab.filename for lab in db_labs}
        for f in sorted(base.glob("*.json")):
            if f.name in db_filenames:
                continue
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
