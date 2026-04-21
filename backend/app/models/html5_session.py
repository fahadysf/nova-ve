from sqlalchemy import Column, String, Integer, DateTime
from app.database import Base
from datetime import datetime


class Html5Session(Base):
    __tablename__ = "html5_sessions"

    username = Column(String(64), primary_key=True)
    connection_id = Column(String(255), primary_key=True)
    pod = Column(Integer, nullable=True)
    token = Column(String(512), nullable=True)
    expires_at = Column(DateTime, nullable=True)
