from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.dependencies import get_current_user, get_current_admin
from app.openapi import COMMON_RESPONSES
from app.schemas.user import UserRead, UserCreate, UserUpdate
from app.services.auth_service import AuthService
from app.services import audit_service
from app.models.user import User

router = APIRouter(prefix="/api/users", tags=["users"], responses=COMMON_RESPONSES)

# #261 — fields a non-admin may change on their own account. Everything else
# in UserUpdate (role, quotas, tenancy) is admin-only; a self-service PATCH
# carrying any of those is rejected instead of silently applied.
SELF_SERVICE_FIELDS = {"email", "name", "password", "html5"}


@router.get("/")
async def list_users(
    current_user: UserRead = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User))
    users = result.scalars().all()
    data = {}
    for u in users:
        data[u.username] = UserRead.model_validate(u).model_dump()
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

    # #261 — create accepts the full account form: forward quota + tenancy
    # fields explicitly instead of letting create_user drop them.
    user = await auth_service.create_user(
        username=request.username,
        password=request.password,
        email=request.email,
        name=request.name,
        role=request.role,
        ram=request.ram,
        cpu=request.cpu,
        sat=request.sat,
        expiration=request.expiration,
        datestart=request.datestart,
        html5=request.html5,
        folder=request.folder,
        pod=request.pod,
    )
    await audit_service.record(
        "user.create",
        actor=current_user.username,
        target_type="user",
        target_id=user.username,
        target_name=user.name,
        detail={"role": user.role},
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

    update_data = request.model_dump(exclude_unset=True)

    # #261 — privilege split: self-service accounts may only touch profile
    # fields; role/quotas/tenancy require the admin guard.
    if current_user.role != "admin":
        escalation = sorted(set(update_data) - SELF_SERVICE_FIELDS)
        if escalation:
            raise HTTPException(
                status_code=403,
                detail=f"Admin role required to change: {', '.join(escalation)}.",
            )

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    # #297: snapshot the fields about to change for the audit trail. The
    # password itself never leaves this handler — only the fact that it
    # changed does.
    before = {field: getattr(user, field) for field in update_data if hasattr(user, field)}
    password_changed = bool(update_data.get("password"))

    if "password" in update_data and update_data["password"]:
        from app.core.security import hash_password
        user.password_hash = hash_password(update_data.pop("password"))

    for field, value in update_data.items():
        if hasattr(user, field) and value is not None:
            setattr(user, field, value)

    await db.commit()
    await db.refresh(user)
    # A save that changed nothing is not an event worth a row.
    changes = audit_service.field_changes(before, {field: getattr(user, field) for field in before})
    if changes or password_changed:
        await audit_service.record(
            "user.update",
            actor=current_user.username,
            target_type="user",
            target_id=user.username,
            target_name=user.name,
            detail={"changes": changes, "password_changed": password_changed},
        )
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
    if current_user.username == username:
        raise HTTPException(status_code=400, detail="You cannot delete your own account.")

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    # #297: describe the account before it disappears — the trail has no
    # foreign key to users and must stand on its own afterwards.
    deleted_role, deleted_name = user.role, user.name
    await db.delete(user)
    await db.commit()
    await audit_service.record(
        "user.delete",
        actor=current_user.username,
        target_type="user",
        target_id=username,
        target_name=deleted_name,
        detail={"role": deleted_role},
    )
    return {
        "code": 200,
        "status": "success",
        "message": f"User {username} deleted successfully.",
    }
