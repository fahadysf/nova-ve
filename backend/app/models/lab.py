from sqlalchemy import Column, String, Integer, Boolean, BigInteger, Text, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
import uuid
from app.database import Base


class LabMeta(Base):
    __tablename__ = "labs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner = Column(String(64), ForeignKey("users.username"), nullable=False)
    filename = Column(String(255), nullable=False)
    name = Column(String(255), nullable=False)
    path = Column(Text, nullable=False)

    author = Column(String(255), default="", nullable=False)
    description = Column(Text, default="", nullable=False)
    body = Column(Text, default="", nullable=False)
    version = Column(String(16), default="0", nullable=False)
    scripttimeout = Column(Integer, default=300, nullable=False)
    countdown = Column(Integer, default=0, nullable=False)
    linkwidth = Column(String(8), default="1", nullable=False)
    grid = Column(Boolean, default=True, nullable=False)
    lock = Column(Boolean, default=False, nullable=False)
