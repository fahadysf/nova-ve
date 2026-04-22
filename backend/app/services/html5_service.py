import base64
import secrets
from datetime import datetime, timedelta, UTC

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.html5_session import Html5Session
from app.schemas.user import UserRead


class Html5SessionService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.settings = get_settings()

    async def create_console_url(
        self,
        current_user: UserRead,
        *,
        host: str,
        port: int,
        protocol: str,
    ) -> str:
        connection_id = self._connection_id(host=host, port=port, protocol=protocol)
        token = secrets.token_hex(32).upper()
        expires_at = datetime.now(UTC) + timedelta(seconds=self.settings.SESSION_MAX_AGE)

        await self.db.execute(
            delete(Html5Session).where(
                Html5Session.username == current_user.username,
                Html5Session.connection_id == connection_id,
            )
        )
        self.db.add(
            Html5Session(
                username=current_user.username,
                connection_id=connection_id,
                pod=getattr(current_user, "pod", 0),
                token=token,
                expires_at=expires_at,
            )
        )
        await self.db.commit()
        return f"/html5/#/client/{connection_id}?token={token}"

    @staticmethod
    def _connection_id(*, host: str, port: int, protocol: str) -> str:
        raw = f"{port}\0{protocol}\0{host}".encode()
        return base64.b64encode(raw).decode()
