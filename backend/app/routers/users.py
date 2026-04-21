from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.dependencies import get_current_user
from app.schemas.user import UserRead
from app.models.user import User

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("/")
async def list_users(
    current_user: UserRead = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User))
    users = result.scalars().all()
    data = {}
    for u in users:
        data[u.username] = {
            "username": u.username,
            "email": u.email,
            "name": u.name,
            "role": u.role,
            "online": 1 if u.online else 0,
            "ip": u.ip,
            "folder": u.folder,
            "lab": u.lab,
            "pod": u.pod,
            "diskusage": u.diskusage,
        }
    return {
        "code": 200,
        "status": "success",
        "message": "Successfully listed users (60040).",
        "data": data,
    }
