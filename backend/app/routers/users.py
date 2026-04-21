from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.dependencies import get_current_user, get_current_admin
from app.schemas.user import UserRead, UserCreate, UserUpdate
from app.services.auth_service import AuthService
from app.models.user import User

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("/")
async def list_users(
    current_user: UserRead = Depends(get_current_admin),
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


@router.post("/")
async def create_user(
    request: UserCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserRead = Depends(get_current_admin),
):
    auth_service = AuthService(db)
    result = await db.execute(select(User).where(User.username == request.username))
    if result.scalar_one_or_none():
        return {
            "code": 400,
            "status": "error",
            "message": f"User {request.username} already exists.",
        }

    user = await auth_service.create_user(
        username=request.username,
        password=request.password,
        email=request.email,
        name=request.name,
        role=request.role,
    )
    return {
        "code": 200,
        "status": "success",
        "message": "User created successfully.",
        "data": UserRead.model_validate(user).model_dump(),
    }


@router.get("/{username}")
async def get_user(
    username: str,
    current_user: UserRead = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if current_user.username != username and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied.")

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    return {
        "code": 200,
        "status": "success",
        "message": "User retrieved successfully.",
        "data": UserRead.model_validate(user).model_dump(),
    }


@router.patch("/{username}")
async def update_user(
    username: str,
    request: UserUpdate,
    current_user: UserRead = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if current_user.username != username and current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied.")

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    update_data = request.model_dump(exclude_unset=True)
    if "password" in update_data and update_data["password"]:
        from app.core.security import hash_password
        user.password_hash = hash_password(update_data.pop("password"))

    for field, value in update_data.items():
        if hasattr(user, field) and value is not None:
            setattr(user, field, value)

    await db.commit()
    await db.refresh(user)
    return {
        "code": 200,
        "status": "success",
        "message": "User updated successfully.",
        "data": UserRead.model_validate(user).model_dump(),
    }


@router.delete("/{username}")
async def delete_user(
    username: str,
    current_user: UserRead = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    await db.delete(user)
    await db.commit()
    return {
        "code": 200,
        "status": "success",
        "message": f"User {username} deleted successfully.",
    }
