#!/usr/bin/env python3
"""Seed script for nova-ve database.

Usage:
    cd backend && source .venv/bin/activate && python -m scripts.seed
"""

import asyncio
import os
import sys
from pathlib import Path

# Ensure backend is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.ext.asyncio import AsyncSession
from app.database import async_session_maker
from app.services.auth_service import AuthService
from app.core.constants import UserRole


def get_default_admin() -> dict[str, str | UserRole]:
    return {
        "username": os.getenv("NOVA_VE_ADMIN_USERNAME", "admin"),
        "password": os.getenv("NOVA_VE_ADMIN_PASSWORD", "admin"),
        "email": os.getenv("NOVA_VE_ADMIN_EMAIL", "admin@nova-ve.local"),
        "name": os.getenv("NOVA_VE_ADMIN_NAME", "Administrator"),
        "role": UserRole.ADMIN,
    }


async def seed_admin_user(db: AsyncSession) -> None:
    auth_service = AuthService(db)
    default_admin = get_default_admin()
    user = await auth_service.create_user(**default_admin)
    print(f"[seed] Created admin user: {user.username}")


async def seed() -> None:
    default_admin = get_default_admin()
    async with async_session_maker() as db:
        async with db.begin():
            auth_service = AuthService(db)
            # Check if the configured bootstrap admin already exists.
            from sqlalchemy import select
            from app.models.user import User
            result = await db.execute(select(User).where(User.username == default_admin["username"]))
            existing = result.scalar_one_or_none()
            if existing:
                print(f"[seed] Admin user {default_admin['username']} already exists, skipping.")
                return

        # Re-open session for seeding (previous was in transaction)
        async with async_session_maker() as db:
            await seed_admin_user(db)
            print("[seed] Done.")


if __name__ == "__main__":
    asyncio.run(seed())
