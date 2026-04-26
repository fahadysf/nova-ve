from pydantic import BaseModel, Field
from typing import Any, Dict, Optional, Literal, List
from uuid import UUID


class NodeBase(BaseModel):
    id: int
    name: str = Field(default="Node")
    type: Literal["qemu", "docker", "iol", "dynamips"] = "qemu"
    template: str = Field(default="")
    image: str = Field(default="")
    console: Literal["telnet", "vnc", "rdp"] = "telnet"
    status: Literal[0, 2] = 0
    delay: int = 0
    cpu: int = 1
    ram: int = 1024
    ethernet: int = 1
    cpulimit: int = 1
    uuid: Optional[UUID] = None
    firstmac: Optional[str] = None
    left: int = 0
    top: int = 0
    icon: str = Field(default="Router.png")
    width: str = Field(default="0")
    config: bool = False
    config_list: List[str] = Field(default_factory=list)
    sat: int = 0
    computed_sat: int = 0
    extras: Dict[str, Any] = Field(default_factory=dict)


class NodeRead(NodeBase):
    url: Optional[str] = Field(default="")
    cpu_usage: Optional[int] = None
    ram_usage: Optional[int] = None
    disk_usage: Optional[str] = None

    class Config:
        from_attributes = True


class NodeCreate(BaseModel):
    name: str
    type: Literal["qemu", "docker", "iol", "dynamips"] = "qemu"
    template: str
    image: str
    console: Literal["telnet", "vnc", "rdp"] = "telnet"
    cpu: int = 1
    ram: int = 1024
    ethernet: int = 1
    delay: int = 0
    left: int = 0
    top: int = 0
    icon: Optional[str] = None
    extras: Dict[str, Any] = Field(default_factory=dict)


class NodeBatchCreate(BaseModel):
    name_prefix: str = Field(default="Node")
    count: int = Field(default=1, ge=1, le=24)
    placement: Literal["grid", "row"] = "grid"
    type: Literal["qemu", "docker", "iol", "dynamips"] = "qemu"
    template: str
    image: str
    console: Literal["telnet", "vnc", "rdp"] = "telnet"
    cpu: int = 1
    ram: int = 1024
    ethernet: int = 1
    delay: int = 0
    left: int = 0
    top: int = 0
    icon: Optional[str] = None
    extras: Dict[str, Any] = Field(default_factory=dict)


class NodeUpdate(BaseModel):
    name: Optional[str] = None
    cpu: Optional[int] = None
    ram: Optional[int] = None
    ethernet: Optional[int] = None
    image: Optional[str] = None
    console: Optional[Literal["telnet", "vnc", "rdp"]] = None
    delay: Optional[int] = None
    left: Optional[int] = None
    top: Optional[int] = None
    icon: Optional[str] = None
    config: Optional[str] = None
    extras: Optional[Dict[str, Any]] = None
