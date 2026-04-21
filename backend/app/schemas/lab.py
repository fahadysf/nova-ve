from pydantic import BaseModel, Field
from typing import Optional, List
from uuid import UUID


class LabMetaBase(BaseModel):
    name: str = Field(default="", max_length=255)
    author: str = Field(default="", max_length=255)
    description: str = Field(default="", max_length=2000)
    body: str = Field(default="", max_length=10000)
    version: str = Field(default="0", max_length=16)
    scripttimeout: int = Field(default=300)
    countdown: int = Field(default=0)
    linkwidth: str = Field(default="1")
    grid: bool = Field(default=True)
    lock: bool = Field(default=False)
    sat: Optional[str] = Field(default="-1")


class LabMetaRead(LabMetaBase):
    id: UUID
    filename: str
    owner: str
    path: str
    shared: List[str] = Field(default_factory=list)

    class Config:
        from_attributes = True


class LabMetaCreate(LabMetaBase):
    path: str
    filename: Optional[str] = None


class LabMetaUpdate(BaseModel):
    name: Optional[str] = None
    author: Optional[str] = None
    description: Optional[str] = None
    body: Optional[str] = None
    version: Optional[str] = None
    scripttimeout: Optional[int] = None
    countdown: Optional[int] = None
    linkwidth: Optional[str] = None
    grid: Optional[bool] = None
    lock: Optional[bool] = None
