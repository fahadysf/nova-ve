# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

import uuid
import time
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.user import User
from app.config import get_settings
from app.core.security import hash_password, verify_password

settings = get_settings()
MIN_SESSION_MAX_AGE = 14400


def session_max_age_seconds() -> int:
    return max(int(settings.SESSION_MAX_AGE), MIN_SESSION_MAX_AGE)


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

    async def create_session(self, user: User, *, ip: str | None = None) -> str:
        token = str(uuid.uuid4())
        now = int(time.time())
        user.session_token = token
        user.session_expires = now + session_max_age_seconds()
        user.online = True
        # #297: last-login tracking for the Users management view. Only this
        # sign-in path stamps it — validate_session runs on every request
        # and must not.
        user.last_login = now
        if ip:
            user.ip = ip
        await self.db.commit()
        return token

    async def validate_session(self, token: str, username: str) -> User | None:
        now = int(time.time())
        result = await self.db.execute(
            select(User).where(
                User.username == username,
                User.session_token == token,
                User.session_expires > now,
            )
        )
        user = result.scalar_one_or_none()
        if user:
            user.session_expires = now + session_max_age_seconds()
            await self.db.commit()
        return user

    async def destroy_session(self, user: User) -> None:
        user.session_token = None
        user.session_expires = None
        user.online = False
        await self.db.commit()
