# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

from sqlalchemy import BigInteger, Column, String, Text
from app.database import Base


class AuditLog(Base):
    """User activity trail (#297) — one row per security-relevant action.

    Deliberately has NO foreign key to ``users``: the trail must survive
    deletion of the account it describes, so ``actor`` is a plain username
    snapshot (NULL for anonymous actors such as failed sign-ins or the
    first-user bootstrap).

    ``created_at`` is epoch seconds (UTC), matching the timestamp
    convention of the ``users`` table; ``id`` provides the intra-second
    ordering. ``event`` is ``<category>.<action>`` and is stored split as
    well so the log view can filter by either half.
    """

    __tablename__ = "audit_log"

    # A plain BIGINT key — BIGSERIAL on PostgreSQL, where the trail lives.
    id = Column(BigInteger, primary_key=True)
    created_at = Column(BigInteger, nullable=False, index=True)
    actor = Column(String(64), nullable=True, index=True)
    event = Column(String(64), nullable=False, index=True)
    category = Column(String(32), nullable=False)
    action = Column(String(32), nullable=False)
    outcome = Column(String(16), nullable=False, default="success")
    target_type = Column(String(32), nullable=True)
    target_id = Column(String(128), nullable=True)
    target_name = Column(String(255), nullable=True)
    lab_path = Column(Text, nullable=True)
    lab_name = Column(String(255), nullable=True)
    ip = Column(String(64), nullable=True)
    detail = Column(Text, nullable=True)
