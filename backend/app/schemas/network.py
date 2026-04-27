from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field


NetworkType = Literal[
    "linux_bridge",
    "ovs_bridge",
    "nat",
    "cloud",
    "management",
    "pnet0", "pnet1", "pnet2", "pnet3", "pnet4",
    "pnet5", "pnet6", "pnet7", "pnet8", "pnet9",
    "internal", "internal2", "internal3",
    "private", "private2", "private3",
    "nat0",
]


class NetworkBase(BaseModel):
    """Persisted network record (v2).

    The ``count`` field has been removed from the persisted shape — it must be
    derived from ``links[]`` on read. ``visibility`` is a boolean; ``implicit``
    flags networks that the loader synthesises (e.g. node-to-node link bridges).
    """

    id: int
    name: str = Field(default="Net")
    type: NetworkType = "linux_bridge"
    left: int = 0
    top: int = 0
    icon: str = Field(default="01-Cloud-Default.svg")
    width: int = 0
    style: str = Field(default="Solid")
    linkstyle: str = Field(default="Straight")
    color: str = Field(default="")
    label: str = Field(default="")
    visibility: bool = True
    implicit: bool = False
    smart: int = -1
    config: Dict[str, Any] = Field(default_factory=dict)


class NetworkRead(NetworkBase):
    """Network with the live, links-derived ``count`` exposed to the API."""

    count: int = 0

    class Config:
        from_attributes = True


class NetworkCreate(BaseModel):
    name: str
    type: NetworkType = "linux_bridge"
    left: int = 0
    top: int = 0


class NetworkUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[NetworkType] = None
    left: Optional[int] = None
    top: Optional[int] = None
    icon: Optional[str] = None
    visibility: Optional[bool] = None
    config: Optional[Dict[str, Any]] = None
