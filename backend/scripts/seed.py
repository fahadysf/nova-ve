#!/usr/bin/env python3
"""Seed script for nova-ve database.

Usage:
    cd backend && source .venv/bin/activate && python -m scripts.seed
"""

import asyncio
import sys
from pathlib import Path

# Ensure backend is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.ext.asyncio import AsyncSession
from app.database import async_session_maker
from app.services.auth_service import AuthService
from app.core.constants import UserRole


DEFAULT_ADMIN = {
    "username": "admin",
    "password": "admin",
    "email": "admin@nova-ve.local",
    "name": "Administrator",
    "role": UserRole.ADMIN,
}


async def seed_admin_user(db: AsyncSession) -> None:
    auth_service = AuthService(db)
    user = await auth_service.create_user(**DEFAULT_ADMIN)
    print(f"[seed] Created admin user: {user.username} / {DEFAULT_ADMIN['password']}")


async def seed() -> None:
    async with async_session_maker() as db:
        async with db.begin():
            auth_service = AuthService(db)
            # Check if admin already exists
            from sqlalchemy import select
            from app.models.user import User
            result = await db.execute(select(User).where(User.username == "admin"))
            existing = result.scalar_one_or_none()
            if existing:
                print("[seed] Admin user already exists, skipping.")
                return

        # Re-open session for seeding (previous was in transaction)
        async with async_session_maker() as db:
            await seed_admin_user(db)
            print("[seed] Done.")


if __name__ == "__main__":
    asyncio.run(seed())
