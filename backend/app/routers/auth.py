from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.schemas.auth import LoginRequest, AuthResponse
from app.schemas.user import UserRead
from app.services.auth_service import AuthService
from app.dependencies import get_current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


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
    response.set_cookie(
        key="nova_session",
        value=token,
        path="/api/",
        httponly=True,
        secure=False,  # Set True in production with HTTPS
        samesite="lax",
        max_age=14400,
    )
    response.set_cookie(
        key="nova_user",
        value=user.username,
        path="/api/",
        httponly=True,
        secure=False,
        samesite="lax",
        max_age=14400,
    )

    return {
        "code": 200,
        "status": "success",
        "message": "User logged in (90013).",
    }


@router.get("")
async def get_auth(
    current_user: UserRead = Depends(get_current_user),
):
    return {
        "code": 200,
        "status": "success",
        "message": "User has been loaded (90002).",
        "data": current_user.model_dump(),
        "eve_uid": "local",
        "eve_expire": "20991231",
    }


@router.get("/logout")
async def logout(
    response: Response,
    current_user: UserRead = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    auth_service = AuthService(db)
    user_model = await auth_service.validate_session(
        current_user.session_token or "", current_user.username
    )
    if user_model:
        await auth_service.destroy_session(user_model)

    response.delete_cookie(key="nova_session", path="/api/")
    response.delete_cookie(key="nova_user", path="/api/")
    return {
        "code": 200,
        "status": "success",
        "message": "User logged out (90014).",
    }
