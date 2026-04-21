from pydantic import BaseModel, Field
from typing import Optional


class UserBase(BaseModel):
    username: str = Field(..., max_length=64)
    email: str = Field(..., max_length=255)
    name: str = Field(..., max_length=255)
    role: str = Field(default="user", pattern=r"^(admin|editor|user)$")
    extauth: str = Field(default="internal")
    ram: int = Field(default=-1)
    cpu: int = Field(default=-1)
    sat: int = Field(default=-1)
    expiration: int = Field(default=-1)
    html5: bool = Field(default=True)
    folder: str = Field(default="/")
    pod: int = Field(default=0)
    diskusage: float = Field(default=0.0)
    online: Optional[int] = Field(default=0)
    ip: Optional[str] = Field(default=None)
    lab: Optional[str] = Field(default=None)


class UserRead(UserBase):
    session: Optional[int] = Field(default=None)
    pexpiration: Optional[int] = Field(default=-1)
    datestart: Optional[int] = Field(default=-1)

    class Config:
        from_attributes = True


class UserCreate(UserBase):
    password: str = Field(..., min_length=6)


class UserUpdate(BaseModel):
    email: Optional[str] = None
    name: Optional[str] = None
    role: Optional[str] = None
    ram: Optional[int] = None
    cpu: Optional[int] = None
    html5: Optional[bool] = None
    password: Optional[str] = None


class UserInDB(UserBase):
    password_hash: Optional[str] = None
