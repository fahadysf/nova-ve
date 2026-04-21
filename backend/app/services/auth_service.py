import uuid
import time
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from argon2 import PasswordHasher
from app.models.user import User

ph = PasswordHasher()


class AuthService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def authenticate(self, username: str, password: str) -> User | None:
        result = await self.db.execute(select(User).where(User.username == username))
        user = result.scalar_one_or_none()
        if not user or not user.password_hash:
            return None
        try:
            ph.verify(user.password_hash, password)
            return user
        except Exception:
            return None

    async def create_session(self, user: User) -> str:
        token = str(uuid.uuid4())
        user.session_token = token
        user.session_expires = int(time.time()) + 14400  # 4h
        user.online = True
        await self.db.commit()
        return token

    async def validate_session(self, token: str, username: str) -> User | None:
        result = await self.db.execute(
            select(User).where(
                User.username == username,
                User.session_token == token,
                User.session_expires > int(time.time()),
            )
        )
        return result.scalar_one_or_none()

    async def destroy_session(self, user: User) -> None:
        user.session_token = None
        user.session_expires = None
        user.online = False
        await self.db.commit()
