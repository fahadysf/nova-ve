from sqlalchemy import Column, String, Integer, Boolean, BigInteger, Float
from app.database import Base


class User(Base):
    __tablename__ = "users"

    username = Column(String(64), primary_key=True, index=True)
    email = Column(String(255), nullable=False)
    name = Column(String(255), nullable=False)
    role = Column(String(16), default="user", nullable=False)
    extauth = Column(String(16), default="internal", nullable=False)
    password_hash = Column(String(255), nullable=True)

    # Quotas
    ram = Column(Integer, default=-1, nullable=False)
    cpu = Column(Integer, default=-1, nullable=False)
    sat = Column(Integer, default=-1, nullable=False)
    expiration = Column(BigInteger, default=-1, nullable=False)
    datestart = Column(BigInteger, default=-1, nullable=False)
    diskusage = Column(Float, default=0.0, nullable=False)

    # Session / UI
    html5 = Column(Boolean, default=True, nullable=False)
    folder = Column(String(512), default="/", nullable=False)
    lab = Column(String(512), nullable=True)
    pod = Column(Integer, default=0, nullable=False)

    # Runtime
    session_token = Column(String(255), nullable=True)
    session_expires = Column(BigInteger, nullable=True)
    ip = Column(String(64), nullable=True)
    online = Column(Boolean, default=False, nullable=False)
