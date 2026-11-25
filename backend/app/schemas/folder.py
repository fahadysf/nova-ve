from pydantic import BaseModel, Field
from typing import List, Optional


class FolderItem(BaseModel):
    name: str
    path: str
    umtime: Optional[int] = None
    mtime: Optional[str] = None
    spy: Optional[int] = -1
    lock: Optional[bool] = False
    shared: Optional[int] = 0
    # Username, "*" (all users — the Shared space) or "system" (#322).
    owner: Optional[str] = None


class LabListItem(BaseModel):
    file: str
    path: str
    umtime: Optional[int] = None
    mtime: Optional[str] = None
    spy: Optional[int] = -1
    lock: Optional[bool] = False
    shared: Optional[int] = 0
    owner: Optional[str] = None


class FolderListResponse(BaseModel):
    folders: List[FolderItem] = Field(default_factory=list)
    labs: List[LabListItem] = Field(default_factory=list)


class FolderCreateRequest(BaseModel):
    path: str = ""
    name: str | None = None


class FolderRenameRequest(BaseModel):
    path: str
