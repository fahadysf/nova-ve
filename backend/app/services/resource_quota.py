import asyncio
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.models.lab import LabMeta
from app.models.user import User
from app.services.lab_service import _normalize_relative_lab_path


class ResourceQuotaError(Exception):
    pass


@dataclass(frozen=True)
class ResourceQuotaContext:
    owner: str
    cpu_limit: int
    ram_limit: int
    lab_owners: dict[str, str]
    lab_files: dict[str, str]


class ResourceQuotaService:
    def __init__(self) -> None:
        self._locks: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, dict[str, asyncio.Lock]
        ] = weakref.WeakKeyDictionary()

    def _lock(self, owner: str) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        locks = self._locks.setdefault(loop, {})
        return locks.setdefault(owner, asyncio.Lock())

    @asynccontextmanager
    async def admission(
        self,
        db: AsyncSession,
        filename: str,
        lab_id: str,
    ) -> AsyncIterator[ResourceQuotaContext | None]:
        normalized = _normalize_relative_lab_path(filename)
        result = await db.execute(
            select(LabMeta)
            .where(LabMeta.filename == normalized)
            .options(load_only(LabMeta.owner))
        )
        lab = result.scalar_one_or_none()
        owner = lab.owner if lab is not None else None
        registered_id = str(lab.id) if lab is not None else None
        # Authentication commits its session update before the handler runs.
        # Release the connection while this request waits on its owner's lock.
        await db.rollback()
        if lab is None:
            # File-only imports predate persisted ownership. Keep their existing
            # behavior: no display/JSON/starter identity is promoted to owner.
            yield None
            return

        if registered_id != lab_id:
            raise ResourceQuotaError("The lab file does not match its registered identity.")
        if not owner:
            raise ResourceQuotaError("The registered lab owner could not be resolved.")
        async with self._lock(owner):
            result = await db.execute(
                select(User).where(User.username == owner).options(load_only(User.cpu, User.ram))
            )
            owner_user = result.scalar_one_or_none()
            if owner_user is None:
                raise ResourceQuotaError("The registered lab owner could not be resolved.")
            cpu_limit, ram_limit = owner_user.cpu, owner_user.ram
            lab_files: dict[str, str] = {}
            if (cpu_limit, ram_limit) != (-1, -1):
                result = await db.execute(
                    select(LabMeta)
                    .where(LabMeta.owner == owner)
                    .options(load_only(LabMeta.id, LabMeta.filename))
                )
                lab_files = {str(row.id): row.filename for row in result.scalars().all()}

            # Keep the owner lock through launch and registration without
            # tying up a database connection while the runtime starts.
            await db.rollback()
            yield ResourceQuotaContext(
                owner=owner,
                cpu_limit=cpu_limit,
                ram_limit=ram_limit,
                lab_owners=dict.fromkeys(lab_files, owner),
                lab_files=lab_files,
            )


resource_quota_service = ResourceQuotaService()
