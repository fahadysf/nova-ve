import uuid
import time
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from argon2 import PasswordHasher
from app.models.user import User
from app.config import get_settings
from app.core.security import hash_password, verify_password

ph = PasswordHasher()
settings = get_settings()


class AuthService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def authenticate(self, username: str, password: str) -> User | None:
        result = await self.db.execute(select(User).where(User.username == username))
        user = result.scalar_one_or_none()
        if not user or not user.password_hash:
            return None
        if verify_password(password, user.password_hash):
            return user
        return None

    async def create_user(
        self,
        username: str,
        password: str,
        email: str,
        name: str,
        role: str = "user",
        **kwargs,
    ) -> User:
        """Create a new user with an Argon2-hashed password."""
        user = User(
            username=username,
            password_hash=hash_password(password),
            email=email,
            name=name,
            role=role,
            **kwargs,
        )
        self.db.add(user)
        await self.db.commit()
        await self.db.refresh(user)
        return user

    async def create_session(self, user: User) -> str:
        token = str(uuid.uuid4())
        user.session_token = token
        user.session_expires = int(time.time()) + settings.SESSION_MAX_AGE
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
