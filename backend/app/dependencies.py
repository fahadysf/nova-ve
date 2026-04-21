from fastapi import Request, HTTPException, status, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.services.auth_service import AuthService
from app.schemas.user import UserRead


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> UserRead:
    """Dependency to extract and validate session cookie."""
    session_cookie = request.cookies.get("nova_session")
    user_cookie = request.cookies.get("nova_user")

    if not session_cookie or not user_cookie:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": 401, "status": "unauthorized", "message": "User is not authenticated or session timed out (90001)."},
        )

    auth_service = AuthService(db)
    user = await auth_service.validate_session(session_cookie, user_cookie)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": 401, "status": "unauthorized", "message": "User is not authenticated or session timed out (90001)."},
        )
    return user
