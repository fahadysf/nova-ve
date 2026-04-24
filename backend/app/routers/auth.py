from datetime import timedelta
from fastapi import APIRouter, Depends, Request, Response, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.schemas.auth import LoginRequest
from app.schemas.user import UserRead, UserCreate
from app.services.auth_service import AuthService
from app.dependencies import get_current_user, get_optional_user
from app.config import get_settings
from app.core.security import create_access_token
from app.core.constants import UserRole

router = APIRouter(prefix="/api/auth", tags=["auth"])
settings = get_settings()
SESSION_MAX_AGE_FLOOR = 14400


def _session_cookie_max_age() -> int:
    return max(int(settings.SESSION_MAX_AGE), SESSION_MAX_AGE_FLOOR)


@router.post("/login")
async def login(
    request: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    auth_service = AuthService(db)
    user = await auth_service.authenticate(request.username, request.password)
    if not user:
        return {
            "code": 401,
            "status": "unauthorized",
            "message": "Invalid credentials (90001).",
        }

    token = await auth_service.create_session(user)
    access_token = create_access_token(
        data={"sub": user.username, "role": user.role},
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )

    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=token,
        path="/api/",
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        max_age=_session_cookie_max_age(),
    )
    response.set_cookie(
        key=settings.SESSION_USER_COOKIE,
        value=user.username,
        path="/api/",
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        max_age=_session_cookie_max_age(),
    )

    return {
        "code": 200,
        "status": "success",
        "message": "User logged in (90013).",
        "data": UserRead.model_validate(user).model_dump(),
        "access_token": access_token,
        "token_type": "bearer",
    }


@router.post("/register")
async def register(
    request: UserCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserRead | None = Depends(get_optional_user),
):
    """Register a new user. Admin only, unless no users exist (first user becomes admin)."""
    from sqlalchemy import select, func
    from app.models.user import User

    auth_service = AuthService(db)

    # Allow first user to register without auth (becomes admin)
    result = await db.execute(select(func.count()).select_from(User))
    user_count = result.scalar()

    if user_count > 0 and (not current_user or current_user.role != UserRole.ADMIN):
        raise HTTPException(
            status_code=403,
            detail={"code": 403, "status": "forbidden", "message": "Admin access required to create users."},
        )

    # Check for duplicate username
    result = await db.execute(select(User).where(User.username == request.username))
    if result.scalar_one_or_none():
        return {
            "code": 400,
            "status": "error",
            "message": f"User {request.username} already exists.",
        }

    role = request.role if current_user and current_user.role == UserRole.ADMIN else UserRole.USER
    if user_count == 0:
        role = UserRole.ADMIN

    user = await auth_service.create_user(
        username=request.username,
        password=request.password,
        email=request.email,
        name=request.name,
        role=role,
    )

    return {
        "code": 200,
        "status": "success",
        "message": "User created successfully.",
        "data": UserRead.model_validate(user).model_dump(),
    }


@router.get("")
async def get_auth(
    current_user: UserRead = Depends(get_current_user),
):
    """Legacy endpoint — kept for compatibility with the upstream API contract."""
    return {
        "code": 200,
        "status": "success",
        "message": "User has been loaded (90002).",
        "data": current_user.model_dump(),
        "eve_uid": "local",
        "eve_expire": "20991231",
    }


@router.get("/me")
async def get_me(
    current_user: UserRead = Depends(get_current_user),
):
    """Current user profile."""
    return {
        "code": 200,
        "status": "success",
        "message": "User has been loaded (90002).",
        "data": current_user.model_dump(),
        "eve_uid": "local",
        "eve_expire": "20991231",
    }


async def _logout(
    response: Response,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    auth_service = AuthService(db)
    session_cookie = request.cookies.get(settings.SESSION_COOKIE_NAME)
    user_cookie = request.cookies.get(settings.SESSION_USER_COOKIE)

    if session_cookie and user_cookie:
        user_model = await auth_service.validate_session(session_cookie, user_cookie)
        if user_model:
            await auth_service.destroy_session(user_model)

    response.delete_cookie(key=settings.SESSION_COOKIE_NAME, path="/api/")
    response.delete_cookie(key=settings.SESSION_USER_COOKIE, path="/api/")
    return {
        "code": 200,
        "status": "success",
        "message": "User logged out (90014).",
    }


@router.post("/logout")
async def logout(
    response: Response,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    return await _logout(response, request, db)


@router.get("/logout")
async def logout_legacy(
    response: Response,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    return await _logout(response, request, db)
