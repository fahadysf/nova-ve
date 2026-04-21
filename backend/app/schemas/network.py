from pydantic import BaseModel, Field
from typing import Optional, Literal


class NetworkBase(BaseModel):
    id: int
    name: str = Field(default="Net")
    type: Literal[
        "bridge", "pnet0", "pnet1", "pnet2", "pnet3", "pnet4",
        "pnet5", "pnet6", "pnet7", "pnet8", "pnet9",
        "internal", "internal2", "internal3",
        "private", "private2", "private3", "nat0"
    ] = "bridge"
    left: int = 0
    top: int = 0
    icon: str = Field(default="01-Cloud-Default.svg")
    width: int = 0
    style: str = Field(default="Solid")
    linkstyle: str = Field(default="Straight")
    color: str = Field(default="")
    label: str = Field(default="")
    visibility: bool = True
    smart: int = -1
    count: int = 0


class NetworkRead(NetworkBase):
    class Config:
        from_attributes = True


class NetworkCreate(BaseModel):
    name: str
    type: Literal[
        "bridge", "pnet0", "pnet1", "pnet2", "pnet3", "pnet4",
        "pnet5", "pnet6", "pnet7", "pnet8", "pnet9",
        "internal", "internal2", "internal3",
        "private", "private2", "private3", "nat0"
    ] = "bridge"
    left: int = 0
    top: int = 0


class NetworkUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    left: Optional[int] = None
    top: Optional[int] = None
    icon: Optional[str] = None
