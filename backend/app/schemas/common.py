from typing import TypeVar, Generic, Optional
from pydantic import BaseModel

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    code: int
    status: str
    message: str
    data: Optional[T] = None
