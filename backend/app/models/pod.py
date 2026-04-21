from sqlalchemy import Column, Integer, String, BigInteger, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from app.database import Base


class Pod(Base):
    __tablename__ = "pods"

    id = Column(Integer, primary_key=True, nullable=False)
    expiration = Column(BigInteger, default=-1, nullable=False)
    username = Column(String(64), ForeignKey("users.username"), nullable=True)
    lab_id = Column(UUID(as_uuid=True), ForeignKey("labs.id"), nullable=True)
