from pydantic import BaseModel
from typing import Optional
from app.schemas.user import UserRead


class LoginRequest(BaseModel):
    username: str
    password: str


class AuthResponse(BaseModel):
    code: int = 200
    status: str = "success"
    message: str
    data: Optional[UserRead] = None
    eve_uid: Optional[str] = None
    eve_expire: Optional[str] = None
