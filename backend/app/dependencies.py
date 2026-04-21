from fastapi import Request, HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.services.auth_service import AuthService
from app.schemas.user import UserRead
from app.core.security import verify_access_token

security_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security_bearer),
    db: AsyncSession = Depends(get_db),
) -> UserRead:
    """Dependency to extract and validate session cookie or JWT Bearer token."""
    # Try cookie session first (legacy platform browser compatibility)
    session_cookie = request.cookies.get("nova_session")
    user_cookie = request.cookies.get("nova_user")

    if session_cookie and user_cookie:
        auth_service = AuthService(db)
        user = await auth_service.validate_session(session_cookie, user_cookie)
        if user:
            return UserRead.model_validate(user)

    # Fall back to JWT Bearer token
    if credentials:
        token = credentials.credentials
        payload = verify_access_token(token)
        username = payload.get("sub")
        if username:
            from sqlalchemy import select
            from app.models.user import User
            result = await db.execute(select(User).where(User.username == username))
            user = result.scalar_one_or_none()
            if user:
                return UserRead.model_validate(user)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": 401, "status": "unauthorized", "message": "User is not authenticated or session timed out (90001)."},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_admin(
    current_user: UserRead = Depends(get_current_user),
) -> UserRead:
    """Dependency to ensure the current user is an admin."""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": 403, "status": "forbidden", "message": "Admin access required."},
        )
    return current_user
